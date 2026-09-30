"""Runtime configuration, read from environment variables (Docker-friendly)."""

import json
import os


def _bool(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


# Danbooru tag category ids
CAT_GENERAL, CAT_ARTIST, CAT_COPYRIGHT, CAT_CHARACTER, CAT_META = 0, 1, 3, 4, 5

# Hydrus namespace -> Danbooru category. Anything not listed is "general" and
# keeps its namespace as part of the tag name (e.g. "species:cat").
DEFAULT_NAMESPACE_MAP = {
    "creator": CAT_ARTIST,
    "artist": CAT_ARTIST,
    "studio": CAT_ARTIST,
    "series": CAT_COPYRIGHT,
    "copyright": CAT_COPYRIGHT,
    "franchise": CAT_COPYRIGHT,
    "character": CAT_CHARACTER,
    "person": CAT_CHARACTER,
    "meta": CAT_META,
    "medium": CAT_META,
}

# Danbooru category -> preferred Hydrus namespace (used when translating
# searches like "artist:foo" or when looking up a Danbooru-style bare tag).
DEFAULT_CATEGORY_NAMESPACE = {
    CAT_ARTIST: "creator",
    CAT_COPYRIGHT: "series",
    CAT_CHARACTER: "character",
    CAT_META: "meta",
}

# Hydrus "rating:" tag values -> Danbooru rating letter
DEFAULT_RATING_TAG_MAP = {
    "general": "g", "safe": "g", "g": "g",
    "sensitive": "s", "s": "s",
    "questionable": "q", "q": "q",
    "explicit": "e", "e": "e", "nsfw": "e",
}


class Config:
    def __init__(self):
        self.hydrus_url = os.environ.get("HYDRUS_URL", "http://127.0.0.1:45869").rstrip("/")
        self.hydrus_key = os.environ.get("HYDRUS_ACCESS_KEY", "")
        self.hydrus_verify_tls = _bool("HYDRUS_VERIFY_TLS", False)
        self.hydrus_timeout = _int("HYDRUS_TIMEOUT", 60)

        self.host = os.environ.get("BRIDGE_HOST", "0.0.0.0")
        self.port = _int("BRIDGE_PORT", 8000)
        # Public base URL that clients use to reach the bridge. Used to build
        # file_url / preview_file_url. If empty, derived from the Host header.
        self.public_url = os.environ.get("BRIDGE_PUBLIC_URL", "").rstrip("/")

        # Optional bridge auth. If set, clients must pass login+api_key
        # (query params or HTTP basic auth) matching these values.
        self.bridge_login = os.environ.get("BRIDGE_LOGIN", "")
        self.bridge_api_key = os.environ.get("BRIDGE_API_KEY", "")

        # Hydrus service selection (hex keys). Empty -> hydrus defaults
        # ("all known tags" and "combined local file domains").
        self.tag_service_key = os.environ.get("HYDRUS_TAG_SERVICE_KEY", "616c6c206b6e6f776e2074616773")
        self.file_service_key = os.environ.get("HYDRUS_FILE_SERVICE_KEY", "")
        # Tag service that Danbooru tag edits (PUT /posts/{id}) are written to.
        self.write_tag_service_key = os.environ.get("HYDRUS_WRITE_TAG_SERVICE_KEY", "6c6f63616c2074616773")
        # Set to false to make the bridge strictly read-only.
        self.allow_writes = _bool("ALLOW_WRITES", True)
        # Name of the like/dislike rating service used for favorites.
        self.favorites_service = os.environ.get("HYDRUS_FAVORITES_SERVICE", "favourites")
        # Name of a numerical rating service used to derive "score" (optional).
        self.score_service = os.environ.get("HYDRUS_SCORE_SERVICE", "")

        self.default_limit = _int("DEFAULT_LIMIT", 20)
        self.max_limit = _int("MAX_LIMIT", 200)
        # Rating used when a file has no rating:* tag ("" -> "q" like an unrated booru post)
        self.default_rating = os.environ.get("DEFAULT_RATING", "q")
        # Max width/height of the "sample" (large_file_url) rendition. 0 disables
        # sample rendering (large_file_url = original file).
        self.sample_size = _int("SAMPLE_SIZE", 850)
        self.search_cache_ttl = _int("SEARCH_CACHE_TTL", 120)
        # Fetch MD5 for every post (1 extra hydrus call per page). Many clients
        # key caches on md5, so this is on by default.
        self.fetch_md5 = _bool("FETCH_MD5", True)
        self.cors = _bool("BRIDGE_CORS", True)
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

        self.namespace_map = dict(DEFAULT_NAMESPACE_MAP)
        self.namespace_map.update(self._json_env("NAMESPACE_MAP"))
        self.category_namespace = dict(DEFAULT_CATEGORY_NAMESPACE)
        self.category_namespace.update({int(k): v for k, v in self._json_env("CATEGORY_NAMESPACE").items()})
        self.rating_tag_map = dict(DEFAULT_RATING_TAG_MAP)
        self.rating_tag_map.update(self._json_env("RATING_TAG_MAP"))
        self.rating_namespace = os.environ.get("RATING_NAMESPACE", "rating")

    @staticmethod
    def _json_env(name):
        raw = os.environ.get(name, "")
        if not raw:
            return {}
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except ValueError:
            raise SystemExit(f"{name} must be a JSON object")
