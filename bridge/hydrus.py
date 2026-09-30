"""Minimal Hydrus Client API client (stdlib only)."""

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

log = logging.getLogger("bridge.hydrus")

KEY_MY_TAGS = "6c6f63616c2074616773"
KEY_ALL_KNOWN_TAGS = "616c6c206b6e6f776e2074616773"
KEY_COMBINED_LOCAL_MEDIA = "616c6c206c6f63616c206d65646961"
KEY_ALL_LOCAL_FILES = "616c6c206c6f63616c2066696c6573"
KEY_MY_FILES = "6c6f63616c2066696c6573"
KEY_TRASH = "7472617368"

SERVICE_NUMERICAL_RATING = 6
SERVICE_LIKE_RATING = 7
SERVICE_INCDEC_RATING = 22

METADATA_BATCH = 256


class HydrusError(Exception):
    def __init__(self, status, message):
        super().__init__(f"hydrus {status}: {message}")
        self.status = status
        self.message = message


class LRU:
    """Small thread-safe LRU cache with per-entry TTL."""

    def __init__(self, maxsize=4096, ttl=300):
        self.maxsize, self.ttl = maxsize, ttl
        self._d = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._d.get(key)
            if item is None:
                return None
            value, expires = item
            if expires < time.monotonic():
                del self._d[key]
                return None
            self._d.move_to_end(key)
            return value

    def set(self, key, value, ttl=None):
        with self._lock:
            self._d[key] = (value, time.monotonic() + (self.ttl if ttl is None else ttl))
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def clear(self):
        with self._lock:
            self._d.clear()


def _encode_params(params):
    out = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, (list, dict)):
            out[k] = json.dumps(v)
        else:
            out[k] = str(v)
    return urllib.parse.urlencode(out, quote_via=urllib.parse.quote)


class HydrusClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.hydrus_url
        self.headers = {"Hydrus-Client-API-Access-Key": cfg.hydrus_key}
        self.ssl_ctx = None
        if self.base.startswith("https"):
            self.ssl_ctx = ssl.create_default_context()
            if not cfg.hydrus_verify_tls:  # hydrus ships a self-signed cert
                self.ssl_ctx.check_hostname = False
                self.ssl_ctx.verify_mode = ssl.CERT_NONE
        self.metadata_cache = LRU(8192, ttl=60)
        self.md5_cache = LRU(65536, ttl=86400)
        self.tag_search_cache = LRU(2048, ttl=300)
        self._services = None
        self._services_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ low level
    def _url(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + _encode_params(params)
        return url

    def open(self, path, params=None, headers=None, method="GET", body=None):
        """Returns the raw urllib response (caller must close). Raises HydrusError."""
        h = dict(self.headers)
        if headers:
            h.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self._url(path, params), data=data, headers=h, method=method)
        try:
            return urllib.request.urlopen(req, timeout=self.cfg.hydrus_timeout, context=self.ssl_ctx)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                return e  # let file proxy relay "range not satisfiable"
            msg = e.reason
            try:
                payload = json.loads(e.read().decode("utf-8", "replace"))
                msg = payload.get("error", msg)
            except Exception:
                pass
            raise HydrusError(e.code, msg) from None
        except urllib.error.URLError as e:
            raise HydrusError(502, f"cannot reach hydrus at {self.base}: {e.reason}") from None

    def get_json(self, path, params=None):
        t = time.monotonic()
        with self.open(path, params) as r:
            data = json.loads(r.read().decode("utf-8"))
        log.debug("GET %s %s (%.0f ms)", path, params, (time.monotonic() - t) * 1000)
        return data

    def post_json(self, path, body):
        with self.open(path, method="POST", body=body) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip() else {}

    # ------------------------------------------------------------------ services
    def services(self):
        """List of {name, service_key, type, ...}; cached for 5 minutes."""
        with self._lock:
            if self._services is not None and time.monotonic() - self._services_at < 300:
                return self._services
        data = self.get_json("/get_services")
        raw = data.get("services_v2") or data.get("services") or {}
        out = []
        if isinstance(raw, list):
            out = raw
        elif isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, list):  # legacy {"local_tags": [...], ...}
                    out.extend(v)
                elif isinstance(v, dict):  # {service_key: {name, type, ...}}
                    out.append(dict(v, service_key=v.get("service_key", k)))
        with self._lock:
            self._services, self._services_at = out, time.monotonic()
        return out

    def find_service(self, name, types=None):
        if not name:
            return None
        for s in self.services():
            if types and s.get("type") not in types:
                continue
            if s.get("name", "").lower() == name.lower() or s.get("service_key") == name:
                return s
        return None

    # ------------------------------------------------------------------ endpoints
    def api_version(self):
        return self.get_json("/api_version")

    def verify_access_key(self):
        return self.get_json("/verify_access_key")

    def search_files(self, tags, sort_type=None, sort_asc=None, file_service_key=None, return_hashes=False):
        params = {"tags": tags, "return_hashes": return_hashes, "return_file_ids": True}
        if sort_type is not None:
            params["file_sort_type"] = sort_type
        if sort_asc is not None:
            params["file_sort_asc"] = sort_asc
        if self.cfg.tag_service_key:
            params["tag_service_key"] = self.cfg.tag_service_key
        fsk = file_service_key or self.cfg.file_service_key
        if fsk:
            params["file_service_key"] = fsk
        return self.get_json("/get_files/search_files", params)

    def file_metadata(self, file_ids, include_notes=False):
        """Returns {file_id: metadata} for the ids (cached)."""
        result, missing = {}, []
        for fid in file_ids:
            m = None if include_notes else self.metadata_cache.get(fid)
            if m is None:
                missing.append(fid)
            else:
                result[fid] = m
        for i in range(0, len(missing), METADATA_BATCH):
            chunk = missing[i:i + METADATA_BATCH]
            data = self.get_json("/get_files/file_metadata", {
                "file_ids": chunk, "include_notes": include_notes or None,
                "include_services_object": False,
            })
            for m in data.get("metadata", []):
                if m.get("file_id") is None:
                    continue
                self.metadata_cache.set(m["file_id"], m)
                result[m["file_id"]] = m
        return result

    def file_metadata_known(self, file_ids, include_notes=False):
        """Like file_metadata, but unknown ids are skipped instead of raising 404."""
        try:
            return self.file_metadata(file_ids, include_notes)
        except HydrusError as e:
            if e.status not in (400, 404):
                raise
        out = {}
        for fid in file_ids:
            try:
                out.update(self.file_metadata([fid], include_notes))
            except HydrusError as e:
                if e.status not in (400, 404):
                    raise
        return out

    def file_metadata_by_hash(self, hashes):
        data = self.get_json("/get_files/file_metadata", {"hashes": list(hashes), "include_services_object": False})
        out = []
        for m in data.get("metadata", []):
            if m.get("file_id") is not None:
                self.metadata_cache.set(m["file_id"], m)
                out.append(m)
        return out

    def md5s(self, sha256s):
        """sha256 -> md5 map, batched and cached (hydrus stores md5 for every import)."""
        result, missing = {}, []
        for h in sha256s:
            v = self.md5_cache.get(h)
            if v is None:
                missing.append(h)
            else:
                result[h] = v
        for i in range(0, len(missing), METADATA_BATCH):
            chunk = missing[i:i + METADATA_BATCH]
            try:
                data = self.get_json("/get_files/file_hashes", {
                    "hashes": chunk, "source_hash_type": "sha256", "desired_hash_type": "md5"})
            except HydrusError as e:
                log.warning("md5 lookup failed: %s", e)
                break
            for k, v in data.get("hashes", {}).items():
                self.md5_cache.set(k, v)
                result[k] = v
        return result

    def search_tags(self, text, tag_service_key=None):
        """[{value, count}] using display tags (what file search matches against)."""
        key = (text, tag_service_key)
        cached = self.tag_search_cache.get(key)
        if cached is not None:
            return cached
        params = {"search": text, "tag_display_type": "display"}
        tsk = tag_service_key or self.cfg.tag_service_key
        if tsk:
            params["tag_service_key"] = tsk
        if self.cfg.file_service_key:
            params["file_service_key"] = self.cfg.file_service_key
        tags = self.get_json("/add_tags/search_tags", params).get("tags", [])
        self.tag_search_cache.set(key, tags)
        return tags

    def siblings_and_parents(self, tags):
        return self.get_json("/add_tags/get_siblings_and_parents", {"tags": list(tags)}).get("tags", {})

    def add_tags(self, file_id, service_key, add=(), delete=()):
        actions = {}
        if add:
            actions["0"] = list(add)
        if delete:
            actions["1"] = list(delete)
        if not actions:
            return
        self.post_json("/add_tags/add_tags", {
            "file_id": file_id, "service_keys_to_actions_to_tags": {service_key: actions}})
        self.metadata_cache.set(file_id, None, ttl=0)

    def associate_url(self, file_id, add=(), delete=()):
        body = {"file_id": file_id}
        if add:
            body["urls_to_add"] = list(add)
        if delete:
            body["urls_to_delete"] = list(delete)
        self.post_json("/add_urls/associate_url", body)
        self.metadata_cache.set(file_id, None, ttl=0)

    def set_rating(self, file_id, service_key, rating):
        self.post_json("/edit_ratings/set_rating", {
            "file_id": file_id, "rating_service_key": service_key, "rating": rating})
        self.metadata_cache.set(file_id, None, ttl=0)

    def set_notes(self, file_id, notes):
        self.post_json("/add_notes/set_notes", {"file_id": file_id, "notes": notes})

    def delete_notes(self, file_id, names):
        self.post_json("/add_notes/delete_notes", {"file_id": file_id, "note_names": list(names)})
