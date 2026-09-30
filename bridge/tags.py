"""Mapping between hydrus tags ("character:hatsune miku") and Danbooru tags
("hatsune_miku", category 4)."""

import fnmatch
import re
import threading
import zlib

from .config import CAT_GENERAL

_WORD_SPLIT = re.compile(r"[_\W]+", re.UNICODE)


def split_namespace(tag):
    if ":" in tag and not tag.startswith(":"):
        ns, sub = tag.split(":", 1)
        return ns, sub
    return "", tag[1:] if tag.startswith(":") else tag


def to_booru_name(text):
    return text.strip().replace(" ", "_").lower()


def to_hydrus_text(name):
    return name.replace("_", " ").strip().lower()


def stable_id(text):
    return zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF or 1


def words(name):
    return [w for w in _WORD_SPLIT.split(name) if w]


class TagMapper:
    def __init__(self, cfg, hydrus):
        self.cfg = cfg
        self.hydrus = hydrus
        self._ids = {}  # stable tag id -> (name, category)
        self._lock = threading.Lock()

    # ----------------------------------------------------------- hydrus -> booru
    def classify(self, hydrus_tag):
        """Returns (danbooru_name, category) or ("rating", letter) for rating tags."""
        ns, sub = split_namespace(hydrus_tag)
        ns_l = ns.lower()
        if ns_l and ns_l == self.cfg.rating_namespace:
            letter = self.cfg.rating_tag_map.get(sub.strip().lower())
            if letter:
                return None, ("rating", letter)
        if ns_l in self.cfg.namespace_map:
            return to_booru_name(sub), self.cfg.namespace_map[ns_l]
        return to_booru_name(hydrus_tag), CAT_GENERAL

    def register(self, name, category):
        tid = stable_id(name)
        with self._lock:
            self._ids[tid] = (name, category)
        return tid

    def lookup_id(self, tid):
        with self._lock:
            return self._ids.get(tid)

    # ----------------------------------------------------------- booru -> hydrus
    def resolve(self, name):
        """All hydrus display tags whose Danbooru form equals `name`, most used
        first. Wildcards (*) expand like Danbooru (max 100 tags)."""
        name = name.lower()
        text = to_hydrus_text(name)
        if "*" in name:
            candidates = self._search(text)
            hits = [(t, c) for t, c in candidates if fnmatch.fnmatchcase(self.classify(t)[0] or "", name)]
            hits.sort(key=lambda x: -x[1])
            return [t for t, _ in hits[:100]]
        # t == text also accepts explicit hydrus namespaces the mapping would
        # rename, e.g. searching "creator:foo" directly.
        hits = [t for t, _ in self._search(text) if t == text or self.classify(t)[0] == name]
        return hits or [text]

    def _search(self, text):
        try:
            return [(t["value"], t.get("count", 0)) for t in self.hydrus.search_tags(text)]
        except Exception:
            return []

    def to_hydrus_for_write(self, name):
        """Hydrus tag to *add* for a Danbooru tag in a tag edit. Accepts Danbooru
        category prefixes (artist:, char:, copy:, meta:, general:)."""
        prefixes = {
            "artist": 1, "art": 1, "copyright": 3, "copy": 3,
            "character": 4, "char": 4, "meta": 5, "general": 0, "gen": 0,
        }
        ns, sub = split_namespace(name)
        if ns.lower() in prefixes:
            cat = prefixes[ns.lower()]
            if cat == 0:
                return to_hydrus_text(sub)
            return f"{self.cfg.category_namespace[cat]}:{to_hydrus_text(sub)}"
        existing = [t for t in self.resolve(name) if self.classify(t)[0] == name.lower()]
        return existing[0] if existing else to_hydrus_text(name)

    def rating_tags(self, letter):
        """Hydrus rating tags that map to a Danbooru rating letter."""
        ns = self.cfg.rating_namespace
        return [f"{ns}:{v}" for v, l in self.cfg.rating_tag_map.items() if l == letter]

    def rating_write_tag(self, letter):
        long = {"g": "general", "s": "sensitive", "q": "questionable", "e": "explicit"}[letter]
        return f"{self.cfg.rating_namespace}:{long}"
