"""A tiny fake of the Hydrus Client API, enough to exercise the bridge end to end."""

import fnmatch
import hashlib
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ACCESS_KEY = "a" * 64
ALL_KNOWN_TAGS = "616c6c206b6e6f776e2074616773"
MY_TAGS = "6c6f63616c2074616773"
FAV_KEY = "f" * 64
SCORE_KEY = "e" * 64


def _file(fid, w, h, mime, ext, tags, urls=(), imported=1700000000, fav=None, stars=None, notes=None, frames=None):
    content = (f"FILE{fid}-" * 200).encode()
    sha = hashlib.sha256(content).hexdigest()
    return {
        "file_id": fid, "hash": sha, "size": len(content), "mime": mime, "ext": ext,
        "width": w, "height": h, "duration": 4040 if mime.startswith("video") else None,
        "num_frames": frames, "has_audio": False, "time_modified": imported + 50,
        "file_services": {"current": {"616c6c206c6f63616c206d65646961": {"time_imported": imported}}, "deleted": {}},
        "known_urls": list(urls), "is_inbox": False, "is_trashed": False, "is_deleted": False,
        "ratings": {FAV_KEY: fav, SCORE_KEY: stars},
        "tags": {ALL_KNOWN_TAGS: {"storage_tags": {"0": list(tags)}, "display_tags": {"0": list(tags)}},
                 MY_TAGS: {"storage_tags": {"0": list(tags)}, "display_tags": {"0": list(tags)}}},
        "notes": notes or {},
        "_content": content, "_md5": hashlib.md5(content).hexdigest(),
    }


FILES = {
    10: _file(10, 1500, 1100, "image/jpeg", ".jpg",
              ["blue eyes", "blonde_hair", "character:hatsune miku", "series:vocaloid", "creator:someartist",
               "meta:highres", "rating:safe", "species:cat"],
              urls=["https://cdn.example.com/a.jpg", "https://www.pixiv.net/artworks/12345"], fav=True, stars=4,
              notes={"translation": "hello"}),
    20: _file(20, 640, 480, "image/png", ".png",
              ["blue eyes", "long hair", "character:kagamine rin", "series:vocaloid", "rating:explicit"],
              imported=1710000000),
    30: _file(30, 1920, 1080, "video/webm", ".webm", ["blue sky", "creator:someartist"], imported=1720000000),
    40: _file(40, 800, 1200, "image/gif", ".gif", ["long hair", "character:hatsune miku", "animated"],
              imported=1730000000, frames=10, fav=False),
}

REQUESTS = []


def _tag_matches(pattern, tag):
    norm = lambda s: s.replace("_", " ")
    return fnmatch.fnmatchcase(norm(tag), norm(pattern))


def _cmp(a, op, b):
    return {"=": a == b, "!=": a != b, ">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b, "~=": abs(a - b) <= b * 0.1}[op]


def _eval(pred, f):
    tags = f["tags"][ALL_KNOWN_TAGS]["display_tags"]["0"]
    if isinstance(pred, list):
        return any(_eval(p, f) for p in pred)
    if pred.startswith("-"):
        return not _eval(pred[1:], f)
    if pred.startswith("system:"):
        s = pred[7:]
        if s == "everything":
            return True
        m = re.match(r"(width|height) (=|!=|>=|<=|>|<) (\d+)$", s)
        if m:
            return _cmp(f[m.group(1)], m.group(2), int(m.group(3)))
        m = re.match(r"filesize (=|!=|>|<) (\d+) B$", s)
        if m:
            return _cmp(f["size"], m.group(1), int(m.group(2)))
        m = re.match(r"hash (=|!=) ([0-9a-f ]+?)( md5)?$", s)
        if m:
            hs = m.group(2).split()
            key = "_md5" if m.group(3) else "hash"
            return (f[key] in hs) == (m.group(1) == "=")
        m = re.match(r"filetype (=|!=) (.+)$", s)
        if m:
            kinds = [k.strip() for k in m.group(2).split(",")]
            hit = f["mime"] in kinds or f["ext"].lstrip(".") in kinds or ("gif" in kinds and f["mime"] == "image/gif")
            return hit == (m.group(1) == "=")
        if s == "rating for favourites is like":
            return f["ratings"][FAV_KEY] is True
        if s == "rating for favourites is dislike":
            return f["ratings"][FAV_KEY] is False
        if s == "does not have a rating for favourites":
            return f["ratings"][FAV_KEY] is None
        m = re.match(r"time imported (>|<|=) (\d{4}-\d\d-\d\d)$", s)
        if m:
            import datetime
            day = datetime.datetime.fromisoformat(m.group(2)).replace(tzinfo=datetime.timezone.utc).timestamp()
            t = f["file_services"]["current"]["616c6c206c6f63616c206d65646961"]["time_imported"]
            return {">": t >= day, "<": t < day, "=": day <= t < day + 86400}[m.group(1)]
        if s in ("has notes", "no notes"):
            return bool(f["notes"]) == (s == "has notes")
        raise ValueError(f"fake hydrus cannot parse {pred}")
    return any(_tag_matches(pred, t) for t in tags)


def public(f):
    return {k: v for k, v in f.items() if not k.startswith("_") and k != "notes"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _args(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        return u.path, q

    def do_GET(self):
        path, q = self._args()
        REQUESTS.append(("GET", path, q))
        if path != "/api_version" and self.headers.get("Hydrus-Client-API-Access-Key") != ACCESS_KEY:
            return self._json({"error": "bad key"}, 403)
        try:
            return self.route(path, q)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)

    def do_POST(self):
        path, _ = self._args()
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        REQUESTS.append(("POST", path, body))
        if path == "/add_tags/add_tags":
            f = FILES[body["file_id"]]
            for svc, actions in body["service_keys_to_actions_to_tags"].items():
                for t in actions.get("0", []):
                    for k in (svc, ALL_KNOWN_TAGS):
                        for kind in ("storage_tags", "display_tags"):
                            lst = f["tags"][k][kind]["0"]
                            if t not in lst:
                                lst.append(t)
                for t in actions.get("1", []):
                    for k in (svc, ALL_KNOWN_TAGS):
                        for kind in ("storage_tags", "display_tags"):
                            lst = f["tags"][k][kind]["0"]
                            if t in lst:
                                lst.remove(t)
        elif path == "/edit_ratings/set_rating":
            FILES[body["file_id"]]["ratings"][body["rating_service_key"]] = body["rating"]
        elif path == "/add_urls/associate_url":
            FILES[body["file_id"]]["known_urls"].extend(body.get("urls_to_add", []))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def route(self, path, q):
        if path == "/api_version":
            return self._json({"version": 95, "hydrus_version": 688})
        if path == "/verify_access_key":
            return self._json({"name": "test", "permits_everything": True, "basic_permissions": [], "human_description": "test key"})
        if path == "/get_services":
            return self._json({"services": [
                {"name": "my tags", "service_key": MY_TAGS, "type": 5},
                {"name": "all known tags", "service_key": ALL_KNOWN_TAGS, "type": 10},
                {"name": "favourites", "service_key": FAV_KEY, "type": 7},
                {"name": "stars", "service_key": SCORE_KEY, "type": 6, "min_stars": 1, "max_stars": 5},
            ]})
        if path == "/get_files/search_files":
            tags = json.loads(q["tags"])
            ids = [fid for fid, f in FILES.items() if all(_eval(p, f) for p in tags)]
            st = int(q.get("file_sort_type", 2))
            asc = q.get("file_sort_asc", "false") == "true"
            keyf = {0: lambda i: FILES[i]["size"], 5: lambda i: FILES[i]["width"], 6: lambda i: FILES[i]["height"]}.get(
                st, lambda i: FILES[i]["file_services"]["current"]["616c6c206c6f63616c206d65646961"]["time_imported"])
            ids.sort(key=keyf, reverse=not asc)
            return self._json({"file_ids": ids})
        if path == "/get_files/file_metadata":
            ids = json.loads(q["file_ids"]) if "file_ids" in q else None
            if ids is None:
                hashes = json.loads(q["hashes"])
                metas = [public(f) for f in FILES.values() if f["hash"] in hashes]
                return self._json({"metadata": metas})
            out = []
            for i in ids:
                if i not in FILES:
                    return self._json({"error": f"file id {i} not found"}, 404)
                m = public(FILES[i])
                if q.get("include_notes") == "true":
                    m["notes"] = FILES[i]["notes"]
                out.append(m)
            return self._json({"metadata": out})
        if path == "/get_files/file_hashes":
            hs = json.loads(q["hashes"])
            return self._json({"hashes": {f["hash"]: f["_md5"] for f in FILES.values() if f["hash"] in hs}})
        if path == "/add_tags/search_tags":
            text = q["search"].replace("_", " ")
            pat = text if "*" in text else text + "*"
            counts = {}
            for f in FILES.values():
                for t in f["tags"][ALL_KNOWN_TAGS]["display_tags"]["0"]:
                    sub = t.split(":", 1)[1] if ":" in t and ":" not in pat else t
                    if fnmatch.fnmatchcase(sub.replace("_", " "), pat) or fnmatch.fnmatchcase(t.replace("_", " "), pat):
                        counts[t] = counts.get(t, 0) + 1
            tags = sorted(({"value": k, "count": v} for k, v in counts.items()), key=lambda d: -d["count"])
            return self._json({"autocomplete_text": {"search_text": text, "inclusive": True}, "tags": tags})
        if path == "/add_tags/get_siblings_and_parents":
            tags = json.loads(q["tags"])
            out = {}
            for t in tags:
                info = {"ideal_tag": t, "siblings": [t], "descendants": [], "ancestors": []}
                if t == "character:hatsune miku":
                    info["siblings"] = [t, "miku", "hatsune_miku"]
                    info["ancestors"] = ["series:vocaloid"]
                out[t] = {MY_TAGS: info}
            return self._json({"tags": out})
        if path in ("/get_files/file", "/get_files/thumbnail", "/get_files/render"):
            f = FILES.get(int(q["file_id"]))
            if not f:
                return self._json({"error": "missing"}, 404)
            data = f["_content"] if path == "/get_files/file" else (path + json.dumps(q, sort_keys=True)).encode()
            ctype = f["mime"] if path == "/get_files/file" else "image/jpeg"
            rng = self.headers.get("Range")
            if rng and path == "/get_files/file":
                a, b = rng.split("=")[1].split("-")
                a, b = int(a), int(b) if b else len(data) - 1
                part = data[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
                data = part
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(data)
            return
        return self._json({"error": f"unknown path {path}"}, 404)


def start(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv
