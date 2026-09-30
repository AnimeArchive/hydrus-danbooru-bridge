"""HTTP front-end: speaks the Danbooru API, answers from Hydrus."""

import base64
import hmac
import json
import logging
import re
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import EMPTY_INDEXES, ApiError, Bridge, FileResult, NotFound, Result
from .config import Config
from .convert import to_xml
from .hydrus import HydrusError

log = logging.getLogger("bridge.http")

FMT = r"(?:\.(?P<fmt>json|xml))?"
ROUTES = []  # (method, regex, handler name)


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + FMT + "$"), fn))
        return fn
    return deco


def flatten(obj, prefix="", out=None):
    """{"search": {"name": "x"}} -> {"search[name]": "x"} (Rails-style params)."""
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}[{k}]" if prefix else str(k), out)
    elif isinstance(obj, list):
        out[prefix] = ",".join(str(x) for x in obj)
    elif isinstance(obj, bool):
        out[prefix] = "true" if obj else "false"
    elif obj is not None:
        out[prefix] = str(obj)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "hydrus-danbooru-bridge/1.0"
    protocol_version = "HTTP/1.1"
    bridge: Bridge = None
    cfg: Config = None

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------ plumbing
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Range")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cors(self):
        if self.cfg.cors:
            self.send_header("Access-Control-Allow-Origin", "*")

    def _params(self):
        url = urllib.parse.urlsplit(self.path)
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype == "application/json":
                try:
                    params.update(flatten(json.loads(raw.decode("utf-8") or "{}")))
                except ValueError:
                    raise ApiError(400, "invalid JSON body")
            elif ctype in ("application/x-www-form-urlencoded", ""):
                body = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                params.update({k: v[-1] for k, v in body.items()})
        method = params.pop("_method", None)
        return url.path, params, method

    def _base_url(self):
        if self.cfg.public_url:
            return self.cfg.public_url
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"localhost:{self.cfg.port}"
        prefix = (self.headers.get("X-Forwarded-Prefix") or "").rstrip("/")
        return f"{proto}://{host.split(',')[0].strip()}{prefix}"

    def _auth(self, params):
        """-> user name, or None for anonymous. Raises 401 on bad credentials."""
        login, key = params.pop("login", None), params.pop("api_key", None)
        params.pop("password_hash", None)
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("basic "):
            try:
                login, key = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)
            except Exception:
                raise ApiError(401, "malformed Authorization header", "SessionLoader::AuthenticationFailure")
        if not self.cfg.bridge_api_key:
            return login or self.cfg.bridge_login or "hydrus"
        if key is None:
            return None
        ok_key = hmac.compare_digest(key, self.cfg.bridge_api_key)
        ok_login = not self.cfg.bridge_login or hmac.compare_digest(login or "", self.cfg.bridge_login)
        if not (ok_key and ok_login):
            raise ApiError(401, "Invalid API key", "SessionLoader::AuthenticationFailure")
        return login or self.cfg.bridge_login or "hydrus"

    def _dispatch(self, method):
        fmt = "json"
        try:
            path, params, override = self._params()
            if override and method == "POST":
                method = override.upper()
            fmt = params.get("format") if params.get("format") in ("json", "xml") else fmt
            if "xml" in (self.headers.get("Accept") or "") and "json" not in (self.headers.get("Accept") or ""):
                fmt = "xml"
            path = path.rstrip("/") or "/"
            for m, rx, fn in ROUTES:
                match = rx.match(path)
                if not match or (m != method and not (m == "GET" and method == "HEAD")):
                    continue
                if match.groupdict().get("fmt"):
                    fmt = match.group("fmt")
                kwargs = {k: v for k, v in match.groupdict().items() if k != "fmt"}
                public = getattr(fn, "public", False)
                user = None
                if not public:
                    user = self._auth(params)
                    if user is None and not getattr(fn, "anonymous_ok", False):
                        raise ApiError(401, "Authentication required (login + api_key)",
                                       "SessionLoader::AuthenticationFailure")
                result = fn(self, params, user, **kwargs)
                if isinstance(result, FileResult):
                    return self._stream(result, method)
                return self._send(result, fmt, method)
            raise NotFound("Page not found")
        except ApiError as e:
            self._error(e.status, e.error, e.message, fmt, method)
        except HydrusError as e:
            status = 404 if e.status == 404 else 503 if e.status in (502, 503) else 502
            if e.status in (401, 403, 419):
                msg = f"hydrus rejected the access key ({e.status}): {e.message}"
            else:
                msg = f"hydrus error: {e.message}"
            self._error(status, "HydrusError", msg, fmt, method)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa
            log.error("unhandled error: %s\n%s", e, traceback.format_exc())
            self._error(500, type(e).__name__, str(e), fmt, method)

    def _send(self, result: Result, fmt, method):
        if result.status == 204 or result.body is None:
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if fmt == "xml":
            body = result.body
            if isinstance(body, dict) and list(body) == [result.root]:
                body = body[result.root]  # {"counts": {...}} -> <counts>...</counts>
            data = to_xml(result.root, body).encode("utf-8")
            ctype = "application/xml; charset=utf-8"
        else:
            data = json.dumps(result.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        self.send_response(result.status)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def _error(self, status, error, message, fmt, method):
        body = {"success": False, "error": error, "message": message, "reason": message, "backtrace": []}
        if fmt == "xml":
            data = (f'<?xml version="1.0" encoding="UTF-8"?>\n<result success="false" '
                    f'reason="{_xml_attr(message)}" message="{_xml_attr(message)}"/>\n').encode("utf-8")
            ctype = "application/xml; charset=utf-8"
        else:
            data = json.dumps(body).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        try:
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream(self, fr: FileResult, method):
        headers = {}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        resp = self.bridge.hydrus.open(fr.path, fr.params, headers=headers)
        try:
            status = getattr(resp, "status", None) or resp.getcode()
            self.send_response(status)
            self._cors()
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
                v = resp.headers.get(h)
                if v:
                    self.send_header(h, v)
            if not resp.headers.get("Accept-Ranges") and fr.path == "/get_files/file":
                self.send_header("Accept-Ranges", "bytes")
            if status < 300:
                self.send_header("Cache-Control", f"public, max-age={fr.cache_seconds}")
            if not resp.headers.get("Content-Length"):
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if method == "HEAD":
                return
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()

    # ------------------------------------------------------------ routes
    @route("GET", r"/posts")
    def r_posts(self, params, user):
        return self.bridge.posts_index(params, self._base_url())

    @route("GET", r"/posts/random")
    def r_posts_random(self, params, user):
        return self.bridge.posts_random(params, self._base_url())

    @route("GET", r"/posts/(?P<post_id>\d+)")
    def r_post(self, params, user, post_id):
        return self.bridge.posts_show(post_id, params, self._base_url())

    @route("PUT", r"/posts/(?P<post_id>\d+)")
    def r_post_update(self, params, user, post_id):
        return self.bridge.posts_update(post_id, params, self._base_url())

    @route("POST", r"/posts/(?P<post_id>\d+)/votes")
    def r_post_vote(self, params, user, post_id):
        return self.bridge.post_vote(post_id, params, self._base_url())

    @route("PUT", r"/posts/(?P<post_id>\d+)/votes")
    def r_post_vote_put(self, params, user, post_id):
        return self.bridge.post_vote(post_id, params, self._base_url())

    @route("POST", r"/posts/(?P<post_id>\d+)/favorite")
    def r_post_fav(self, params, user, post_id):
        return self.bridge.favorites_create({"post_id": post_id}, self._base_url())

    @route("DELETE", r"/posts/(?P<post_id>\d+)/favorite")
    def r_post_unfav(self, params, user, post_id):
        return self.bridge.favorites_delete(post_id)

    @route("GET", r"/counts/posts")
    def r_counts(self, params, user):
        return self.bridge.counts(params)

    @route("GET", r"/explore/posts/popular")
    def r_popular(self, params, user):
        return self.bridge.popular(params, self._base_url())

    @route("GET", r"/explore/posts/(?:viewed|curated)")
    def r_viewed(self, params, user):
        return self.bridge.popular(params, self._base_url())

    @route("GET", r"/tags")
    def r_tags(self, params, user):
        return self.bridge.tags_index(params)

    @route("GET", r"/tags/autocomplete")
    def r_tags_ac(self, params, user):
        return self.bridge.legacy_tag_autocomplete(params)

    @route("GET", r"/tags/(?P<tag_id>\d+)")
    def r_tag(self, params, user, tag_id):
        return self.bridge.tags_show(tag_id)

    @route("GET", r"/autocomplete")
    def r_autocomplete(self, params, user):
        return self.bridge.autocomplete(params)

    @route("GET", r"/related_tag")
    def r_related(self, params, user):
        return self.bridge.related_tag(params)

    @route("GET", r"/tag_aliases")
    def r_aliases(self, params, user):
        return self.bridge.tag_aliases(params)

    @route("GET", r"/tag_implications")
    def r_implications(self, params, user):
        return self.bridge.tag_implications(params)

    @route("GET", r"/artists")
    def r_artists(self, params, user):
        return self.bridge.artists_index(params)

    @route("GET", r"/artists/(?P<artist_id>\d+)")
    def r_artist(self, params, user, artist_id):
        return self.bridge.artists_show(artist_id)

    @route("GET", r"/notes")
    def r_notes(self, params, user):
        return self.bridge.notes_index(params)

    @route("GET", r"/notes/(?P<note_id>\d+)")
    def r_note(self, params, user, note_id):
        return self.bridge.notes_show(note_id)

    @route("GET", r"/favorites")
    def r_favs(self, params, user):
        return self.bridge.favorites_index(params)

    @route("POST", r"/favorites")
    def r_fav_create(self, params, user):
        return self.bridge.favorites_create(params, self._base_url())

    @route("DELETE", r"/favorites/(?P<post_id>\d+)")
    def r_fav_delete(self, params, user, post_id):
        return self.bridge.favorites_delete(post_id)

    @route("GET", r"/profile")
    def r_profile(self, params, user):
        return self.bridge.profile(user)
    r_profile.anonymous_ok = True

    @route("GET", r"/users")
    def r_users(self, params, user):
        return self.bridge.users_index(params, user)

    @route("GET", r"/users/(?P<uid>\d+)")
    def r_user(self, params, user, uid):
        return self.bridge.users_show(uid, user)

    @route("GET", r"/(?P<resource>[a-z_]+)")
    def r_empty_index(self, params, user, resource):
        if resource in EMPTY_INDEXES:
            return Result([], resource.replace("_", "-"))
        raise NotFound("Page not found")

    @route("GET", r"/(?P<resource>[a-z_]+)/(?P<rid>[^/]+)")
    def r_empty_show(self, params, user, resource, rid):
        raise NotFound()

    @route("GET", r"/data/(?P<variant>original|sample|180x180|360x360|720x720)/(?P<file_id>\d+)/(?P<sig>[0-9a-zp]+)/[^/]+")
    def r_data(self, params, user, variant, file_id, sig):
        return self.bridge.data(variant, file_id, sig)
    r_data.public = True

    @route("GET", r"/(?:status|healthz)")
    def r_status(self, params, user):
        return self.bridge.status()
    r_status.public = True

    @route("GET", r"/")
    def r_root(self, params, user):
        return Result({"name": "hydrus-danbooru-bridge", "api": "danbooru",
                       "endpoints": ["/posts.json", "/posts/{id}.json", "/posts/random.json",
                                     "/counts/posts.json", "/tags.json", "/autocomplete.json",
                                     "/related_tag.json", "/tag_aliases.json", "/tag_implications.json",
                                     "/artists.json", "/notes.json", "/favorites.json",
                                     "/profile.json", "/explore/posts/popular.json"]}, "bridge")
    r_root.public = True


def _xml_attr(s):
    return str(s).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


# ROUTES was filled in class-definition order; move the generic catch-alls last.
ROUTES.sort(key=lambda r: r[2].__name__ in ("r_empty_index", "r_empty_show"))


def serve(cfg=None):
    cfg = cfg or Config()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bridge = Bridge(cfg)
    Handler.bridge, Handler.cfg = bridge, cfg
    if not cfg.hydrus_key:
        log.warning("HYDRUS_ACCESS_KEY is not set; hydrus will reject requests")
    try:
        info = bridge.hydrus.verify_access_key()
        log.info("connected to hydrus at %s: %s", cfg.hydrus_url, info.get("human_description", info))
    except HydrusError as e:
        log.warning("hydrus not reachable yet (%s); will retry per request", e)
    if not cfg.bridge_api_key:
        log.warning("BRIDGE_API_KEY is not set: anyone who can reach port %s can read your library", cfg.port)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    httpd.daemon_threads = True
    log.info("danbooru API listening on http://%s:%s", cfg.host, cfg.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
