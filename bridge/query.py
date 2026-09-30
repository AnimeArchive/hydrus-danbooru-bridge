"""Danbooru search query -> Hydrus search_files predicates.

The Danbooru query is parsed into a boolean expression tree (AND / OR / NOT,
parentheses, legacy ~tag OR-groups), metatags are translated to hydrus
`system:` predicates, bare tags are resolved to their hydrus (namespaced)
forms, and the result is converted to conjunctive normal form, which is the
shape hydrus accepts: a list whose items are tags (AND) or lists of tags (OR).
"""

import datetime
import logging
import re
from fractions import Fraction

from .hydrus import KEY_TRASH
from .tags import to_hydrus_text

log = logging.getLogger("bridge.query")

MAX_CLAUSES = 512


class QueryError(Exception):
    pass


# ============================================================ expression nodes
class Node:
    pass


class Const(Node):
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return "TRUE" if self.value else "FALSE"


TRUE, FALSE = Const(True), Const(False)


class Leaf(Node):
    """A hydrus predicate. `neg` builds the negated node (None = cannot negate)."""

    def __init__(self, pred, neg=None):
        self.pred, self._neg = pred, neg

    def negate(self):
        if self._neg is None:
            log.info("cannot negate %r; ignoring it", self.pred)
            return TRUE
        return self._neg() if callable(self._neg) else self._neg

    def __repr__(self):
        return f"Leaf({self.pred!r})"


class And(Node):
    def __init__(self, items):
        self.items = list(items)


class Or(Node):
    def __init__(self, items):
        self.items = list(items)


class Not(Node):
    def __init__(self, item):
        self.item = item


class Directive(Node):
    """Top-level-only instruction (sort, limit, id range, file domain)."""

    def __init__(self, kind, value=None):
        self.kind, self.value = kind, value


def tag_leaf(tag):
    return Leaf(tag, neg=lambda: Leaf("-" + tag, neg=Leaf(tag)))


def sys_leaf(pred, neg_pred):
    return Leaf(pred, neg=lambda: Leaf(neg_pred, neg=Leaf(pred)))


# ============================================================ numeric helpers
COMPLEMENT = {"=": "!=", "!=": "=", ">": "<=", "<": ">=", ">=": "<", "<=": ">", "~": "!="}
HYDRUS_OP = {"=": "=", "!=": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<=", "~": "~="}


def cmp_node(make, op, n, allowed, step=1):
    """Leaf for `quantity op n`, rewriting ops the hydrus predicate lacks."""
    if op not in allowed:
        if op == ">=":
            return cmp_node(make, ">", n - step, allowed, step)
        if op == "<=":
            return cmp_node(make, "<", n + step, allowed, step)
        if op == "!=":
            return Or([cmp_node(make, "<", n, allowed, step), cmp_node(make, ">", n, allowed, step)])
        if op == "=":
            return And([cmp_node(make, ">", n - step, allowed, step), cmp_node(make, "<", n + step, allowed, step)])
    return Leaf(make(op, n), neg=lambda: cmp_node(make, COMPLEMENT[op], n, allowed, step))


def check(value, op, n):
    return {"=": value == n, "!=": value != n, ">": value > n, "<": value < n,
            ">=": value >= n, "<=": value <= n}[op]


_RANGE_OP = re.compile(r"^(>=|<=|>|<|=)?(.*)$")


def parse_range(value, conv):
    """Danbooru range syntax -> list of (op, number) constraints ANDed together,
    or ("in", [numbers]) for comma lists."""
    v = value.strip()
    if "," in v and ".." not in v:
        return [("in", [conv(x) for x in v.split(",") if x])]
    if "..." in v:
        lo, hi = v.split("...", 1)
        out = []
        if lo:
            out.append((">=", conv(lo)))
        if hi:
            out.append(("<", conv(hi)))
        return out
    if ".." in v:
        lo, hi = v.split("..", 1)
        out = []
        if lo:
            out.append((">=", conv(lo)))
        if hi:
            out.append(("<=", conv(hi)))
        return out
    m = _RANGE_OP.match(v)
    op, rest = m.group(1) or "=", m.group(2)
    return [(op, conv(rest))]


def range_node(value, conv, build):
    """build(op, n) -> Node. Combines parse_range constraints."""
    parts = []
    for op, n in parse_range(value, conv):
        if op == "in":
            parts.append(Or([build("=", x) for x in n]))
        else:
            parts.append(build(op, n))
    return parts[0] if len(parts) == 1 else And(parts)


def const_node(value, spec, conv=float):
    """Metatag on a quantity that is the same for every hydrus file."""
    ok = True
    for op, n in parse_range(spec, conv):
        ok = ok and (value in n if op == "in" else check(value, op, n))
    return TRUE if ok else FALSE


_SIZE = re.compile(r"^\s*([\d.]+)\s*([kmg]?)i?b?\s*$", re.I)


def parse_filesize(s):
    m = _SIZE.match(s)
    if not m:
        raise QueryError(f"bad filesize: {s}")
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


_AGE = re.compile(r"^\s*([\d.]+)\s*(s|sec|seconds?|mi|min|minutes?|h|hours?|d|days?|w|weeks?|mo|months?|y|years?)\s*$", re.I)


def parse_age_hours(s):
    m = _AGE.match(s)
    if not m:
        raise QueryError(f"bad age: {s}")
    n, unit = float(m.group(1)), m.group(2).lower()
    if unit.startswith("s"):
        h = n / 3600
    elif unit.startswith("mi"):
        h = n / 60
    elif unit.startswith("mo"):
        h = n * 24 * 30
    elif unit.startswith("h"):
        h = n
    elif unit.startswith("d"):
        h = n * 24
    elif unit.startswith("w"):
        h = n * 24 * 7
    else:
        h = n * 24 * 365
    return max(1, round(h))


def fmt_hours(h):
    d, h = divmod(int(h), 24)
    return f"{d} days {h} hours" if d else f"{h} hours"


def parse_date(s):
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().toordinal()
        except ValueError:
            pass
    raise QueryError(f"bad date: {s}")


def fmt_date(ordinal):
    return datetime.date.fromordinal(int(ordinal)).isoformat()


def ratio_value(s):
    s = s.strip()
    for sep in (":", "/"):
        if sep in s:
            a, b = s.split(sep, 1)
            return float(a) / float(b)
    return float(s)


def ratio_str(x):
    f = Fraction(x).limit_denominator(100)
    return f"{f.numerator}:{f.denominator}"


# ============================================================ filetypes
FILETYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png, apng",
    "gif": "gif", "webp": "image/webp, animated webp", "avif": "avif",
    "jxl": "jxl", "bmp": "bitmap", "mp4": "video/mp4", "webm": "video/webm",
    "mkv": "matroska", "mov": "quicktime", "swf": "flash", "zip": "ugoira",
    "mp3": "mp3", "ogg": "ogg", "flac": "flac", "pdf": "pdf", "psd": "psd",
    "video": "video", "image": "image", "audio": "audio", "animation": "animation",
}


def filetype_node(value):
    kinds = []
    for ext in value.lower().split(","):
        ext = ext.strip().lstrip(".")
        if ext not in FILETYPES:
            raise QueryError(f"unknown filetype: {ext}")
        kinds.append(FILETYPES[ext])
    joined = ", ".join(kinds)
    return sys_leaf(f"system:filetype = {joined}", f"system:filetype != {joined}")


# ============================================================ order: mapping
# value -> (hydrus file_sort_type, ascending) ; None = bridge-side id sort
SORTS = {
    "id": ("local", "id_asc"), "id_asc": ("local", "id_asc"), "id_desc": ("local", "id_desc"),
    "created_at": (2, False), "created_at_asc": (2, True),
    "created_at_desc": (2, False), "import": (2, False), "import_asc": (2, True),
    "change": (14, False), "change_asc": (14, True),
    "change_desc": (14, False), "modified": (14, False), "modified_asc": (14, True),
    "filesize": (0, False), "filesize_asc": (0, True),
    "mpixels": (8, False), "mpixels_asc": (8, True),
    "landscape": (7, False), "portrait": (7, True),
    "tagcount": (9, False), "tagcount_asc": (9, True),
    "duration": (1, False), "duration_asc": (1, True),
    "md5": (20, False), "md5_asc": (20, True),
    "random": (4, False),
    # Danbooru popularity sorts -> hydrus view statistics (closest analogue)
    "score": (10, False), "score_asc": (10, True),
    "favcount": (10, False), "favcount_asc": (10, True),
    "rank": (10, False), "upvotes": (10, False), "downvotes": (10, True),
    # hydrus-only extras
    "views": (10, False), "views_asc": (10, True),
    "viewtime": (11, False), "viewtime_asc": (11, True),
    "width": (5, False), "width_asc": (5, True),
    "height": (6, False), "height_asc": (6, True),
    "framerate": (15, False), "framerate_asc": (15, True),
    "frames": (16, False), "frames_asc": (16, True),
    "archived": (19, False), "archived_asc": (19, True),
    "last_viewed": (18, False), "last_viewed_asc": (18, True),
    "bitrate": (12, False), "bitrate_asc": (12, True),
    "hue": (27, True), "lightness": (23, False), "lightness_asc": (23, True),
    "custom": ("local", "custom"), "none": ("local", "id_desc"),
}

USER_METATAGS = {"user", "approver", "commenter", "comm", "noter", "noteupdater", "flagger",
                 "appealer", "commentaryupdater", "upvote", "downvote", "ordvote", "search",
                 "disapproved", "favgroup", "ordfavgroup", "ordpool"}
ZERO_COUNT_METATAGS = {"comments", "deleted_comments", "active_comments", "flags", "appeals",
                       "replacements", "pools", "series_pools", "collection_pools",
                       "deleted_pools", "active_pools", "child_count", "deleted_child_count",
                       "active_child_count", "approvals", "disapprovals", "upvotes", "downvotes"}
TAGCOUNT_NS = {"gentags": "unnamespaced", "arttags": 1, "copytags": 3, "chartags": 4, "metatags": 5}

METATAGS = ({"rating", "order", "limit", "random", "id", "md5", "width", "height", "ratio",
             "mpixels", "filesize", "duration", "filetype", "date", "age", "score", "favcount",
             "status", "is", "has", "source", "parent", "child", "pool", "fav", "ordfav",
             "tagcount", "notes", "note", "comment", "commentary", "pixiv", "pixiv_id",
             "embedded", "exif", "ai", "system"}
            | USER_METATAGS | ZERO_COUNT_METATAGS | set(TAGCOUNT_NS))


# ============================================================ tokenizer
def tokenize(query):
    tokens, cur, quote = [], [], False
    for ch in query:
        if ch == '"':
            quote = not quote
            continue
        if ch.isspace() and not quote:
            if cur:
                tokens.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        tokens.append("".join(cur))

    out, depth = [], 0
    for tok in tokens:
        while tok:
            if tok == "(":
                out.append("(")
                depth += 1
                tok = ""
            elif tok[:2] in ("-(", "~(") and tok[1:].count("(") > tok[1:].count(")"):
                out += [tok[0], "("]
                depth += 1
                tok = tok[2:]
            elif tok[0] == "(" and tok.count("(") > tok.count(")"):
                out.append("(")
                depth += 1
                tok = tok[1:]
            else:
                break
        closes = 0
        while tok.endswith(")") and depth > 0 and (tok == ")" or tok.count(")") > tok.count("(")):
            tok = tok[:-1]
            closes += 1
            depth -= 1
        if tok:
            out.append(tok)
        out += [")"] * closes
    return out


# ============================================================ compiled query
class Query:
    def __init__(self):
        self.tags = []            # hydrus search_files "tags" argument
        self.impossible = False
        self.sort_type = None     # hydrus file_sort_type
        self.sort_asc = False
        self.local_sort = "id_desc"
        self.custom_ids = None
        self.limit = None         # limit:N (per page)
        self.random_count = None  # random:N
        self.id_filters = []      # [(op, n, negate)]
        self.file_service_key = None

    @property
    def is_random(self):
        return self.sort_type == 4

    def cache_key(self):
        import json
        return json.dumps([self.tags, self.sort_type, self.sort_asc, self.local_sort,
                           self.custom_ids, self.random_count, self.id_filters,
                           self.file_service_key], sort_keys=True)


class Compiler:
    def __init__(self, cfg, hydrus, mapper):
        self.cfg, self.hydrus, self.mapper = cfg, hydrus, mapper

    # -------------------------------------------------------------- parse
    def compile(self, query_string):
        toks = tokenize(query_string or "")
        self._toks, self._i = toks, 0
        tree = self._parse_or()
        if self._i < len(self._toks):  # stray ')'
            log.info("unbalanced ')' in query %r", query_string)
        q = Query()
        items, stack = [], [tree]
        while stack:  # flatten nested top-level ANDs so their directives are seen
            node = stack.pop(0)
            if isinstance(node, And):
                stack[:0] = node.items
            else:
                items.append(node)
        rest = []
        for node in items:
            neg = False
            inner = node
            if isinstance(inner, Not) and isinstance(inner.item, Directive):
                neg, inner = True, inner.item
            if isinstance(inner, Directive):
                self._apply_directive(q, inner, neg)
            else:
                rest.append(node)
        clauses = self._to_cnf(And(rest))
        if clauses is None:
            q.impossible = True
            return q
        q.tags = [c[0] if len(c) == 1 else c for c in clauses] or ["system:everything"]
        if q.random_count and not q.is_random:
            q.sort_type, q.local_sort = 4, None
        return q

    def _peek(self):
        return self._toks[self._i] if self._i < len(self._toks) else None

    def _next(self):
        t = self._peek()
        self._i += 1
        return t

    def _parse_or(self):
        left = self._parse_and()
        while self._peek() is not None and self._peek().lower() == "or":
            self._next()
            left = Or([left, self._parse_and()])
        return left

    def _parse_and(self):
        items, tildes = [], []
        while True:
            t = self._peek()
            if t is None or t == ")" or t.lower() == "or":
                break
            if t == "~" or (t.startswith("~") and len(t) > 1):
                self._next()
                tildes.append(self._parse_unary_rest(t[1:]))
            elif t == "-":
                self._next()
                items.append(Not(self._parse_unary()))
            else:
                items.append(self._parse_unary())
        if tildes:
            items.append(Or(tildes))
        return items[0] if len(items) == 1 else And(items)

    def _parse_unary_rest(self, rest):
        return self._parse_unary() if rest == "" else self._term_or_neg(rest)

    def _parse_unary(self):
        t = self._next()
        if t == "(":
            node = self._parse_or()
            if self._peek() == ")":
                self._next()
            return node
        if t == "-":
            return Not(self._parse_unary())
        return self._term_or_neg(t)

    def _term_or_neg(self, t):
        if t.startswith("-") and len(t) > 1:
            return Not(self.term(t[1:]))
        return self.term(t)

    # -------------------------------------------------------------- terms
    def term(self, tok):
        if ":" in tok:
            key, val = tok.split(":", 1)
            k = key.lower()
            if k in METATAGS and val != "":
                try:
                    return self.metatag(k, val)
                except (QueryError, ValueError, ZeroDivisionError) as e:
                    raise QueryError(f"invalid metatag {tok!r}: {e}")
        hits = self.mapper.resolve(tok)
        if not hits:
            return FALSE
        leaves = [tag_leaf(h) for h in hits]
        return leaves[0] if len(leaves) == 1 else Or(leaves)

    def metatag(self, k, v):
        vl = v.lower()
        if k == "system":  # raw hydrus predicate passthrough: system:width_>_1000
            return Leaf("system:" + v.replace("_", " "))
        if k == "order":
            if vl not in SORTS:
                raise QueryError(f"unknown order {v}")
            return Directive("order", vl)
        if k == "limit":
            return Directive("limit", int(v))
        if k == "random":
            return Directive("random", int(v))
        if k == "id":
            return self._id(v)
        if k == "md5":
            hashes = " ".join(h.strip().lower() for h in v.split(",") if h.strip())
            return sys_leaf(f"system:hash = {hashes} md5", f"system:hash != {hashes} md5")
        if k in ("width", "height"):
            full = {"=", "!=", ">", "<", ">=", "<="}
            return range_node(v, lambda s: int(float(s)), lambda op, n: cmp_node(
                lambda o, x: f"system:{k} {HYDRUS_OP[o]} {int(x)}", op, n, full))
        if k == "filesize":
            rel = {"=", "!=", ">", "<"}

            def build(op, n):
                if op == "=":  # Danbooru matches exact filesizes within +-5%
                    return And([cmp_node(fs, ">", int(n * 0.95), rel), cmp_node(fs, "<", int(n * 1.05), rel)])
                return cmp_node(fs, op, n, rel)
            fs = lambda o, x: f"system:filesize {HYDRUS_OP[o]} {int(x)} B"
            return range_node(v, parse_filesize, build)
        if k == "duration":
            full = {"=", "!=", ">", "<", ">=", "<="}
            return range_node(v, lambda s: int(float(s.rstrip("s")) * 1000), lambda op, n: cmp_node(
                lambda o, x: f"system:duration {HYDRUS_OP[o]} {int(x)} milliseconds", op, n, full))
        if k == "mpixels":
            rel = {"=", "!=", ">", "<"}
            return range_node(v, lambda s: int(float(s) * 1_000_000), lambda op, n: cmp_node(
                lambda o, x: f"system:num pixels {HYDRUS_OP[o]} {int(x)} px", op, n, rel))
        if k == "ratio":
            return range_node(v, ratio_value, self._ratio)
        if k == "tagcount" or k in TAGCOUNT_NS:
            label = "number of tags"
            if k in TAGCOUNT_NS:
                ns = TAGCOUNT_NS[k]
                ns = ns if isinstance(ns, str) else self.cfg.category_namespace[ns]
                label = f"number of {ns} tags"
            rel = {"=", "!=", ">", "<"}
            return range_node(v, lambda s: int(s), lambda op, n: cmp_node(
                lambda o, x: f"system:{label} {HYDRUS_OP[o]} {int(x)}", op, n, rel))
        if k == "notes":
            ex = {"=", ">", "<"}
            return range_node(v, lambda s: int(s), lambda op, n: cmp_node(
                lambda o, x: f"system:num notes {HYDRUS_OP[o]} {int(x)}", op, n, ex))
        if k == "filetype":
            return filetype_node(v)
        if k == "rating":
            return Or([self._rating(r) for r in vl.split(",") if r])
        if k == "date":
            return range_node(v, parse_date, lambda op, n: cmp_node(self._date_pred, op, n, {"=", ">", "<"}))
        if k == "age":
            rel = {"~", ">", "<"}
            return range_node(v, parse_age_hours, lambda op, n: cmp_node(
                lambda o, x: f"system:time imported {HYDRUS_OP[o]} {fmt_hours(x)}", "~" if op == "=" else op, n, rel))
        if k == "score":
            return self._score(v)
        if k == "favcount":
            fav = self._fav_leaf()
            yes, no = const_node(1, v) is TRUE, const_node(0, v) is TRUE
            if yes and no:
                return TRUE
            if not yes and not no:
                return FALSE
            if fav is None:
                return TRUE if no else FALSE
            return fav if yes else Not(fav)
        if k in ("fav", "ordfav"):
            fav = self._fav_leaf()
            return fav if fav is not None else FALSE
        if k == "status":
            return self._status(vl)
        if k == "is":
            return self._is(vl)
        if k == "has":
            return self._has(vl)
        if k == "source":
            return self._source(v)
        if k in ("parent", "child", "pool"):
            return TRUE if vl == "none" else FALSE
        if k in ("pixiv", "pixiv_id"):
            if vl == "any":
                return sys_leaf("system:has domain pixiv.net", "system:does not have domain pixiv.net")
            if vl == "none":
                return sys_leaf("system:does not have domain pixiv.net", "system:has domain pixiv.net")
            rx = rf"pixiv\.net/.*(artworks/|illust_id=){int(v)}(\D|$)"
            return sys_leaf(f"system:has url matching regex {rx}", f"system:does not have a url matching regex {rx}")
        if k == "exif":
            return sys_leaf("system:has exif", "system:no exif")
        if k == "commentary":
            return TRUE if vl == "false" else FALSE
        if k == "embedded":
            return TRUE if vl in ("false", "no") else FALSE
        if k in ("note", "comment", "ai"):
            return FALSE
        if k in USER_METATAGS:
            if k == "user":
                return TRUE  # every hydrus file belongs to the single local user
            return TRUE if vl == "none" else FALSE
        if k in ZERO_COUNT_METATAGS:
            return const_node(0, v)
        raise QueryError(f"unsupported metatag {k}")

    def _id(self, v):
        spec = parse_range(v, int)
        if len(spec) == 1 and spec[0][0] in ("=", "in"):
            ids = spec[0][1] if spec[0][0] == "in" else [spec[0][1]]
            meta = self.hydrus.file_metadata_known(ids)
            hashes = [meta[i]["hash"] for i in ids if i in meta]
            if not hashes:
                return FALSE
            h = " ".join(hashes)
            node = sys_leaf(f"system:hash = {h}", f"system:hash != {h}")
            if len(ids) > 1:
                return And([node, Directive("custom_ids", ids)])
            return node
        return Directive("id_range", spec)

    def _ratio(self, op, x):
        def make(o, val):
            r = ratio_str(val)
            if o in (">", ">="):
                return f"system:ratio wider than {r}"
            if o in ("<", "<="):
                return f"system:ratio taller than {r}"
            return f"system:ratio = {r}"
        return cmp_node(make, op, x, {"=", ">", "<", ">=", "<="}, step=0)

    @staticmethod
    def _date_pred(op, ordinal):
        # hydrus "> date" means "since that day" (inclusive), so strict ">" shifts a day
        if op == ">":
            return f"system:time imported > {fmt_date(ordinal + 1)}"
        if op == "<":
            return f"system:time imported < {fmt_date(ordinal)}"
        return f"system:time imported = {fmt_date(ordinal)}"

    def _rating(self, r):
        letter = {"general": "g", "safe": "g", "sensitive": "s", "questionable": "q", "explicit": "e"}.get(r, r)
        if letter not in "gsqe" or len(letter) != 1:
            raise QueryError(f"unknown rating {r}")
        options = [tag_leaf(t) for t in self.mapper.rating_tags(letter)]
        if letter == self.cfg.default_rating:
            # files with no rating tag at all are reported with the default rating
            options.append(Not(tag_leaf(f"{self.cfg.rating_namespace}:*")))
        return Or(options)

    def _fav_leaf(self):
        from .hydrus import SERVICE_LIKE_RATING
        svc = self.hydrus.find_service(self.cfg.favorites_service, {SERVICE_LIKE_RATING})
        if not svc:
            return None
        name = svc["name"]
        like = f"system:rating for {name} is like"
        return Leaf(like, neg=lambda: Or([
            Leaf(f"system:does not have a rating for {name}"),
            Leaf(f"system:rating for {name} is dislike")]))

    def _score(self, v):
        from .hydrus import SERVICE_NUMERICAL_RATING, SERVICE_INCDEC_RATING
        svc = self.hydrus.find_service(self.cfg.score_service, {SERVICE_NUMERICAL_RATING, SERVICE_INCDEC_RATING})
        if not svc:
            return const_node(0, v)
        name = svc["name"]
        numerical = svc.get("type") == SERVICE_NUMERICAL_RATING
        max_stars = svc.get("max_stars", 5)
        unrated = Leaf(f"system:does not have a rating for {name}",
                       neg=Leaf(f"system:has a rating for {name}"))

        def make(o, n):
            n = int(n)
            val = f"{n}/{max_stars}" if numerical else str(n)
            return f"system:rating for {name} {HYDRUS_OP[o]} {val}"

        def build(op, n):
            node = cmp_node(make, op, n, {"=", ">", "<"})
            if numerical and check(0, op, n):  # unrated counts as score 0
                return Or([node, unrated])
            return node
        return range_node(v, lambda s: int(float(s)), build)

    def _status(self, v):
        if v in ("any", "all", "active"):
            return TRUE
        if v == "deleted":
            return Directive("domain", KEY_TRASH)
        return FALSE

    def _is(self, v):
        if v in ("general", "sensitive", "questionable", "explicit"):
            return self._rating(v)
        if v == "sfw":
            return Or([self._rating("g"), self._rating("s")])
        if v == "nsfw":
            return Or([self._rating("q"), self._rating("e")])
        if v in FILETYPES:
            return filetype_node(v)
        if v in ("active",):
            return TRUE
        if v == "deleted":
            return Directive("domain", KEY_TRASH)
        if v == "inbox":
            return sys_leaf("system:inbox", "system:archive")
        if v in ("archive", "archived"):
            return sys_leaf("system:archive", "system:inbox")
        if v == "favorited":
            return self._fav_leaf() or FALSE
        return FALSE  # parent, child, pending, flagged, banned, appealed, modqueue, ...

    def _has(self, v):
        if v == "source":
            return sys_leaf("system:has urls", "system:no urls")
        if v == "notes":
            return sys_leaf("system:has notes", "system:no notes")
        if v == "audio":
            return sys_leaf("system:has audio", "system:no audio")
        if v == "duration":
            return sys_leaf("system:has duration", "system:no duration")
        if v == "tags":
            return sys_leaf("system:has tags", "system:no tags")
        return FALSE  # children, parent, pools, comments, commentary, ...

    def _source(self, v):
        if v.lower() == "none":
            return sys_leaf("system:no urls", "system:has urls")
        rx = "(?i)^" + ".*".join(re.escape(p) for p in v.split("*"))
        if not v.endswith("*") and "*" in v:
            rx += "$"
        return sys_leaf(f"system:has url matching regex {rx}",
                        f"system:does not have a url matching regex {rx}")

    # -------------------------------------------------------------- directives
    def _apply_directive(self, q, d, negated):
        if d.kind == "order" and not negated:
            kind, val = SORTS[d.value]
            if kind == "local":
                q.local_sort, q.sort_type = val, None
            else:
                q.sort_type, q.sort_asc, q.local_sort = kind, val, None
        elif d.kind == "limit" and not negated:
            q.limit = d.value
        elif d.kind == "random" and not negated:
            q.random_count = d.value
        elif d.kind == "custom_ids" and not negated:
            q.custom_ids = d.value
        elif d.kind == "id_range":
            for op, n in d.value:
                q.id_filters.append((op, n, negated))
        elif d.kind == "domain":
            if not negated:
                q.file_service_key = d.value

    # -------------------------------------------------------------- CNF
    def _nnf(self, node, neg=False):
        if isinstance(node, Const):
            return Const(node.value != neg)
        if isinstance(node, Directive):
            return TRUE  # directives only apply at top level
        if isinstance(node, Leaf):
            return self._nnf(node.negate()) if neg else node
        if isinstance(node, Not):
            return self._nnf(node.item, not neg)
        items = [self._nnf(i, neg) for i in node.items]
        is_and = isinstance(node, And) != neg
        return And(items) if is_and else Or(items)

    def _cnf(self, node):
        """-> list of clauses (list of preds); [] = TRUE; None = FALSE."""
        if isinstance(node, Const):
            return [] if node.value else None
        if isinstance(node, Leaf):
            return [[node.pred]]
        if isinstance(node, And):
            out = []
            for i in node.items:
                c = self._cnf(i)
                if c is None:
                    return None
                out.extend(c)
            return out
        # Or: distribute
        acc = None  # FALSE
        for i in node.items:
            c = self._cnf(i)
            if c == []:
                return []  # TRUE absorbs
            if c is None:
                continue
            if acc is None:
                acc = c
                continue
            acc = [a + b for a in acc for b in c]
            if len(acc) > MAX_CLAUSES:
                raise QueryError("query too complex")
        return acc

    def _to_cnf(self, tree):
        clauses = self._cnf(self._nnf(tree))
        if clauses is None:
            return None
        out, seen = [], set()
        for c in clauses:
            c = list(dict.fromkeys(c))
            s = set(c)
            if any(("-" + p) in s for p in s if not p.startswith("-")):
                continue  # tautology (x or -x)
            key = frozenset(s)
            if key not in seen:
                seen.add(key)
                out.append(c)
        return out
