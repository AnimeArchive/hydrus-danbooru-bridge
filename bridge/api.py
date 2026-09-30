"""Danbooru endpoint implementations on top of the Hydrus client."""

import datetime
import difflib
import fnmatch
import logging
import math
import zlib
from collections import Counter

from .convert import Converter, apply_only, boot_ts, import_time, is_still, parse_only, ts
from .hydrus import HydrusClient, HydrusError, LRU, SERVICE_LIKE_RATING
from .query import Compiler, QueryError, check
from .tags import TagMapper, split_namespace, stable_id, to_booru_name, to_hydrus_text, words

log = logging.getLogger("bridge.api")


class ApiError(Exception):
    def __init__(self, status, message, error="ApiError"):
        super().__init__(message)
        self.status, self.message, self.error = status, message, error


class NotFound(ApiError):
    def __init__(self, message="That record was not found."):
        super().__init__(404, message, "ActiveRecord::RecordNotFound")


class Result:
    """JSON-able body plus the XML root element name."""

    def __init__(self, body, root, status=200):
        self.body, self.root, self.status = body, root, status


class FileResult:
    def __init__(self, path, params, cache_seconds):
        self.path, self.params, self.cache_seconds = path, params, cache_seconds


STATIC_METATAGS = {
    "order": ["id", "id_desc", "score", "score_asc", "favcount", "created_at", "created_at_asc",
              "change", "change_asc", "filesize", "filesize_asc", "mpixels", "mpixels_asc",
              "landscape", "portrait", "tagcount", "tagcount_asc", "duration", "duration_asc",
              "random", "md5", "views", "viewtime", "width", "height", "archived", "last_viewed"],
    "rating": ["general", "sensitive", "questionable", "explicit"],
    "status": ["any", "active", "deleted"],
    "is": ["sfw", "nsfw", "general", "sensitive", "questionable", "explicit", "inbox", "archive",
           "favorited", "deleted", "jpg", "png", "gif", "webp", "mp4", "webm", "zip"],
    "has": ["source", "notes", "audio", "duration", "tags"],
    "filetype": ["jpg", "png", "gif", "webp", "avif", "mp4", "webm", "zip", "swf"],
    "source": ["none"],
}
EMPTY_INDEXES = {"pools", "wiki_pages", "comments", "post_versions", "forum_topics", "forum_posts",
                 "artist_commentaries", "favorite_groups", "dmails", "post_votes", "uploads",
                 "saved_searches", "bulk_update_requests", "pool_versions", "note_versions",
                 "wiki_page_versions", "artist_versions", "post_flags", "post_appeals",
                 "post_approvals", "post_replacements", "iqdb_queries", "media_assets"}


def _p(params, name, default=None):
    v = params.get(name)
    return default if v in (None, "") else v


def _bool(v):
    return str(v).lower() in ("1", "true", "t", "yes", "y", "on")


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.hydrus = HydrusClient(cfg)
        self.mapper = TagMapper(cfg, self.hydrus)
        self.compiler = Compiler(cfg, self.hydrus, self.mapper)
        self.conv = Converter(cfg, self.hydrus, self.mapper)
        self.search_cache = LRU(256, ttl=cfg.search_cache_ttl)

    # =============================================================== helpers
    def _limit(self, params, default=None, maximum=None):
        try:
            n = int(_p(params, "limit", default or self.cfg.default_limit))
        except ValueError:
            n = default or self.cfg.default_limit
        if n <= 0:
            n = default or self.cfg.default_limit
        return min(n, maximum or self.cfg.max_limit)

    def compile(self, tags):
        try:
            return self.compiler.compile(tags)
        except QueryError as e:
            raise ApiError(422, str(e), "PostQuery::Error")

    def search_ids(self, q, use_cache=True):
        if q.impossible:
            return []
        key = q.cache_key()
        if use_cache and not q.is_random:
            hit = self.search_cache.get(key)
            if hit is not None:
                return hit
        log.info("hydrus search: %s sort=%s asc=%s", q.tags, q.sort_type, q.sort_asc)
        res = self.hydrus.search_files(q.tags, sort_type=q.sort_type if q.sort_type is not None else 2,
                                       sort_asc=q.sort_asc, file_service_key=q.file_service_key)
        ids = res.get("file_ids", [])
        for op, n, negate in q.id_filters:
            ids = [i for i in ids if check(i, op, n) != negate]
        if q.local_sort == "id_desc":
            ids = sorted(ids, reverse=True)
        elif q.local_sort == "id_asc":
            ids = sorted(ids)
        elif q.local_sort == "custom" and q.custom_ids:
            order = {v: i for i, v in enumerate(q.custom_ids)}
            ids = sorted(ids, key=lambda i: order.get(i, 1 << 60))
        if q.random_count:
            ids = ids[:q.random_count]
        if not q.is_random:
            self.search_cache.set(key, ids)
        return ids

    def paginate(self, ids, q, params, limit):
        page = str(_p(params, "page", "1")).strip()
        if page[:1] in ("a", "b") and page[1:].isdigit():
            cursor = int(page[1:])
            if page[0] == "b":
                return [i for i in ids if i < cursor][:limit]
            above = [i for i in ids if i > cursor]
            if q.local_sort == "id_desc":
                return above[-limit:]
            return above[:limit]
        try:
            n = max(1, int(page))
        except ValueError:
            raise ApiError(400, f"invalid page: {page}")
        return ids[(n - 1) * limit:n * limit]

    def load_posts(self, ids, base):
        meta = self.hydrus.file_metadata_known(ids)
        metas = [meta[i] for i in ids if i in meta]
        return self.conv.posts(metas, base)

    def get_meta(self, post_id, include_notes=False):
        try:
            fid = int(post_id)
        except (TypeError, ValueError):
            raise NotFound()
        meta = self.hydrus.file_metadata_known([fid], include_notes).get(fid)
        if not meta:
            raise NotFound()
        return meta

    @staticmethod
    def only(body, params):
        spec = _p(params, "only")
        return apply_only(body, parse_only(spec)) if spec else body

    def lookup_tags(self, pattern, categories=None):
        """Danbooru name pattern (with * wildcards; no * = exact) ->
        {(name, category): post_count} using hydrus tag autocomplete."""
        pattern = to_booru_name(pattern)
        text = to_hydrus_text(pattern)
        if not text.strip("* "):
            text = "*"
        try:
            hits = self.hydrus.search_tags(text)
        except HydrusError as e:
            log.warning("tag search failed: %s", e)
            hits = []
        agg = self.conv.aggregate_tags(hits)
        out = {}
        for (name, cat), count in agg.items():
            if categories is not None and cat not in categories:
                continue
            if "*" in pattern:
                if not fnmatch.fnmatchcase(name, pattern):
                    continue
            elif name != pattern:
                continue
            out[(name, cat)] = count
        return out

    # =============================================================== posts
    def posts_index(self, params, base):
        md5 = _p(params, "md5")
        if md5:
            q = self.compile(f"md5:{md5}")
            ids = self.search_ids(q)
            if not ids:
                raise NotFound()
            return Result(self.only(self.load_posts(ids[:1], base)[0], params), "post")
        q = self.compile(_p(params, "tags", ""))
        if _bool(_p(params, "random", "false")):
            q.sort_type, q.local_sort = 4, None
        limit = self._limit(params, q.limit)
        ids = self.paginate(self.search_ids(q), q, params, limit)
        return Result(self.only(self.load_posts(ids, base), params), "posts")

    def posts_random(self, params, base):
        q = self.compile(_p(params, "tags", ""))
        q.sort_type, q.local_sort, q.random_count = 4, None, 1
        ids = self.search_ids(q)
        if not ids:
            raise NotFound()
        return Result(self.only(self.load_posts(ids[:1], base)[0], params), "post")

    def posts_show(self, post_id, params, base):
        meta = self.get_meta(post_id)
        return Result(self.only(self.conv.posts([meta], base)[0], params), "post")

    def posts_update(self, post_id, params, base):
        self._require_writes()
        meta = self.get_meta(post_id)
        fid = meta["file_id"]
        wkey = self.cfg.write_tag_service_key
        current_post = self.conv.posts([meta], base)[0]
        current = set(current_post["tag_string"].split())

        new_string = params.get("post[tag_string]")
        old_string = params.get("post[old_tag_string]")
        rating = params.get("post[rating]")
        source = params.get("post[source]")
        add, remove = set(), set()

        if new_string is not None:
            wanted, explicit_removed = set(), set()
            for tok in new_string.split():
                low = tok.lower()
                if low.startswith("rating:"):
                    rating = low.split(":", 1)[1][:1]
                    continue
                if low.startswith("source:"):
                    source = tok.split(":", 1)[1]
                    continue
                if low.startswith("-"):
                    explicit_removed.add(self._edit_name(low[1:]))
                    continue
                if ":" in low and split_namespace(low)[0] in ("parent", "child", "pool", "newpool", "fav", "favgroup", "status"):
                    continue
                wanted.add(low)
            wanted_names = {self._edit_name(t): t for t in wanted}
            if old_string is not None:
                old = {self._edit_name(t) for t in old_string.split()}
                add_names = set(wanted_names) - old
                remove_names = (old - set(wanted_names)) | explicit_removed
            else:
                add_names = set(wanted_names) - current
                remove_names = (current - set(wanted_names)) | explicit_removed
            add = {self.mapper.to_hydrus_for_write(wanted_names[n]) for n in add_names if n not in current}
            storage = ((meta.get("tags") or {}).get(wkey) or {}).get("storage_tags") or {}
            for t in storage.get("0", []):
                name, _ = self.mapper.classify(t)
                if name and name in remove_names:
                    remove.add(t)
            skipped = remove_names - {self.mapper.classify(t)[0] for t in remove}
            if skipped:
                log.info("post %s: tags %s are not on the write service and cannot be removed", fid, sorted(skipped))

        if rating:
            letter = rating.lower()[:1]
            if letter not in "gsqe":
                raise ApiError(422, f"invalid rating {rating}")
            storage = ((meta.get("tags") or {}).get(wkey) or {}).get("storage_tags") or {}
            ns = self.cfg.rating_namespace + ":"
            remove |= {t for t in storage.get("0", []) if t.startswith(ns)}
            add.add(self.mapper.rating_write_tag(letter))
            remove.discard(self.mapper.rating_write_tag(letter))

        if add or remove:
            self.hydrus.add_tags(fid, wkey, add=sorted(add), delete=sorted(remove - add))
        if source and source not in (meta.get("known_urls") or []):
            self.hydrus.associate_url(fid, add=[source])
        self.search_cache.clear()
        self.hydrus.metadata_cache.set(fid, None, ttl=0)
        return self.posts_show(fid, {}, base)

    def _edit_name(self, tok):
        ns, sub = split_namespace(tok)
        if ns in ("artist", "art", "copyright", "copy", "character", "char", "meta", "general", "gen"):
            return to_booru_name(sub)
        return to_booru_name(tok)

    def counts(self, params):
        q = self.compile(_p(params, "tags", ""))
        q.random_count = None
        if q.is_random:
            q.sort_type, q.local_sort = 2, "id_desc"
        return Result({"counts": {"posts": len(self.search_ids(q))}}, "counts")

    def popular(self, params, base):
        scale = _p(params, "scale", "day")
        try:
            day = datetime.date.fromisoformat(_p(params, "date", datetime.date.today().isoformat())[:10])
        except ValueError:
            raise ApiError(400, "invalid date")
        if scale == "week":
            start = day - datetime.timedelta(days=day.weekday())
            end = start + datetime.timedelta(days=7)
        elif scale == "month":
            start = day.replace(day=1)
            end = (start + datetime.timedelta(days=32)).replace(day=1)
        else:
            start, end = day, day + datetime.timedelta(days=1)
        q = self.compile(f"date:{start.isoformat()}...{end.isoformat()} order:views")
        limit = self._limit(params)
        ids = self.paginate(self.search_ids(q), q, params, limit)
        return Result(self.only(self.load_posts(ids, base), params), "posts")

    # =============================================================== tags
    def tags_index(self, params):
        cats = _p(params, "search[category]")
        categories = {int(c) for c in str(cats).split(",") if c.strip().isdigit()} if cats else None
        order = _p(params, "search[order]", "count")
        limit = self._limit(params, 20, 1000)
        names = _p(params, "search[name]") or _p(params, "search[name_normalize]") or _p(params, "search[name_comma]")
        pattern = (_p(params, "search[name_matches]") or _p(params, "search[name_or_alias_matches]")
                   or _p(params, "search[name_like]"))
        fuzzy = _p(params, "search[fuzzy_name_matches]")
        found = {}
        if names:
            for n in str(names).replace(" ", ",").split(","):
                if n.strip():
                    found.update(self.lookup_tags(n.strip(), categories))
        elif fuzzy:
            f = to_booru_name(fuzzy)
            for key, count in self.lookup_tags(f[: max(1, len(f) // 2)] + "*", categories).items():
                if difflib.SequenceMatcher(None, f, key[0]).ratio() >= 0.6:
                    found[key] = count
            order = "similarity"
        elif pattern:
            found = self.lookup_tags(pattern, categories)
        else:
            found = self.lookup_tags("*", categories)
        if _bool(_p(params, "search[hide_empty]", "false")) or _p(params, "search[post_count]"):
            spec = _p(params, "search[post_count]", ">0")
            from .query import parse_range
            try:
                cons = parse_range(spec, int)
            except ValueError:
                cons = [(">", 0)]
            found = {k: c for k, c in found.items()
                     if all((c in n) if op == "in" else check(c, op, n) for op, n in cons)}
        items = list(found.items())
        if order == "name":
            items.sort(key=lambda kv: kv[0][0])
        elif order == "similarity" and fuzzy:
            f = to_booru_name(fuzzy)
            items.sort(key=lambda kv: -difflib.SequenceMatcher(None, f, kv[0][0]).ratio())
        else:
            items.sort(key=lambda kv: (-kv[1], kv[0][0]))
        page = self._page_number(params)
        items = items[(page - 1) * limit:page * limit]
        body = [self.conv.tag(n, c, cnt) for (n, c), cnt in items]
        return Result(self.only(body, params), "tags")

    def _page_number(self, params):
        try:
            return max(1, int(_p(params, "page", "1")))
        except ValueError:
            return 1

    def tags_show(self, tag_id):
        try:
            hit = self.mapper.lookup_id(int(tag_id))
        except ValueError:
            hit = None
        if not hit:
            raise NotFound()
        name, cat = hit
        count = self.lookup_tags(name).get((name, cat), 0)
        return Result(self.conv.tag(name, cat, count), "tag")

    def autocomplete(self, params):
        query = _p(params, "search[query]") or _p(params, "query") or _p(params, "search[name_matches]") or ""
        kind = _p(params, "search[type]", "tag_query")
        limit = self._limit(params, 10, 1000)
        q = query.strip().lower()
        prefix = ""
        while q[:1] in ("-", "~"):
            prefix, q = prefix + q[0], q[1:]
        if not q:
            return Result([], "autocomplete")
        if kind in ("tag_query", "tag") and ":" in q:
            meta, val = q.split(":", 1)
            if meta in STATIC_METATAGS:
                opts = [o for o in STATIC_METATAGS[meta] if o.startswith(val)]
                return Result([{"type": "static", "label": o, "value": f"{meta}:{o}"} for o in opts[:limit]], "autocomplete")
        if kind in ("tag_query", "tag", "artist", "tag_word", "tag-word"):
            cats = {1} if kind == "artist" else None
            if "*" in q:
                found = self.lookup_tags(q, cats)
            else:
                # prefix matches, plus word-start matches if hydrus returns them
                # ("hair" -> "hair_ornament", "blue_hair"), like Danbooru's tag-word
                try:
                    hits = self.hydrus.search_tags(to_hydrus_text(q))
                except HydrusError:
                    hits = []
                found = {k: c for k, c in self.conv.aggregate_tags(hits).items()
                         if (cats is None or k[1] in cats)
                         and (k[0].startswith(q) or any(w.startswith(q) for w in words(k[0])))}
            items = sorted(found.items(), key=lambda kv: (-kv[1], kv[0][0]))[:limit]
            out = []
            for (name, cat), count in items:
                if kind == "artist":
                    out.append({"type": "artist", "label": name.replace("_", " "), "value": name, "category": cat})
                    continue
                out.append({"type": "tag" if name.startswith(q) else "tag-word",
                            "label": name.replace("_", " "), "value": name,
                            "category": cat, "post_count": count,
                            "tag": self.conv.tag(name, cat, count)})
            return Result(out, "autocomplete")
        return Result([], "autocomplete")

    def legacy_tag_autocomplete(self, params):
        """/tags/autocomplete.json (pre-2022 Danbooru) -> array of tag objects."""
        pattern = _p(params, "search[name_matches]") or _p(params, "search[name]") or _p(params, "query") or ""
        if pattern and "*" not in pattern:
            pattern += "*"
        return self.tags_index({"search[name_matches]": pattern, "limit": _p(params, "limit", "10"),
                                "search[order]": "count"})

    def related_tag(self, params):
        query = _p(params, "query") or _p(params, "search[query]") or ""
        cat = _p(params, "category") or _p(params, "search[category]")
        cat_names = {"general": 0, "artist": 1, "copyright": 3, "character": 4, "meta": 5}
        category = cat_names.get(str(cat).lower(), int(cat) if cat and str(cat).isdigit() else None) if cat else None
        limit = self._limit(params, 25, 1000)
        q = self.compile(query)
        ids = self.search_ids(q)
        total = len(ids)
        sample_ids = ids[:300]
        metas = self.hydrus.file_metadata_known(sample_ids)
        counter, cats = Counter(), {}
        for m in metas.values():
            file_cats, _ = self.conv.file_tags(m)
            for c, names in file_cats.items():
                for n in names:
                    counter[n] += 1
                    cats[n] = c
        n_sample = max(1, len(metas))
        related = []
        for name, c in counter.most_common():
            if category is not None and cats[name] != category:
                continue
            post_count = self.lookup_tags(name).get((name, cats[name]), c)
            freq = c / n_sample
            overlap_est = freq * total
            cos = overlap_est / math.sqrt(total * post_count) if total and post_count else 0.0
            union = total + post_count - overlap_est
            related.append({
                "tag": self.conv.tag(name, cats[name], post_count),
                "cosine_similarity": min(cos, 1.0),
                "jaccard_similarity": min(overlap_est / union, 1.0) if union > 0 else 0.0,
                "overlap_coefficient": min(overlap_est / min(total, post_count), 1.0) if total and post_count else 0.0,
                "frequency": freq,
            })
            if len(related) >= limit:
                break
        qname = to_booru_name(query.strip()) if query.strip() else ""
        body = {"query": query, "post_count": total,
                "tag": self.conv.tag(qname, cats.get(qname, 0), total) if qname and " " not in query.strip() else None,
                "related_tags": related, "wiki_page_tags": []}
        return Result(body, "related-tag")

    def _aliases_and_implications(self, params):
        name = (_p(params, "search[antecedent_name]") or _p(params, "search[consequent_name]")
                or _p(params, "search[name_matches]") or _p(params, "search[antecedent_name_matches]")
                or _p(params, "search[consequent_name_matches]"))
        if not name:
            return [], []
        hydrus_tags = self.mapper.resolve(name)[:20]
        try:
            data = self.hydrus.siblings_and_parents(hydrus_tags)
        except HydrusError as e:
            log.warning("siblings lookup failed: %s", e)
            return [], []
        aliases, implications = {}, {}
        for tag, per_service in data.items():
            if not isinstance(per_service, dict):
                continue
            for info in per_service.values():
                if not isinstance(info, dict):
                    continue
                ideal = self.mapper.classify(info.get("ideal_tag") or tag)[0]
                for s in info.get("siblings", []):
                    a = self.mapper.classify(s)[0]
                    if a and ideal and a != ideal:
                        aliases[(a, ideal)] = True
                me = self.mapper.classify(tag)[0]
                for anc in info.get("ancestors", []):
                    c = self.mapper.classify(anc)[0]
                    if me and c and me != c:
                        implications[(me, c)] = True
                for desc in info.get("descendants", []):
                    a = self.mapper.classify(desc)[0]
                    if me and a and me != a:
                        implications[(a, me)] = True

        def rows(pairs):
            ant = _p(params, "search[antecedent_name]")
            con = _p(params, "search[consequent_name]")
            out = []
            for a, c in pairs:
                if ant and a != to_booru_name(ant):
                    continue
                if con and c != to_booru_name(con):
                    continue
                out.append({"id": stable_id(f"{a}->{c}"), "antecedent_name": a, "reason": "",
                            "creator_id": 1, "consequent_name": c, "status": "active",
                            "forum_topic_id": None, "created_at": boot_ts(), "updated_at": boot_ts(),
                            "approver_id": 1, "forum_post_id": None})
            return out
        return rows(aliases), rows(implications)

    def tag_aliases(self, params):
        return Result(self._aliases_and_implications(params)[0], "tag-aliases")

    def tag_implications(self, params):
        return Result(self._aliases_and_implications(params)[1], "tag-implications")

    # =============================================================== artists
    def _artist(self, name):
        return {"id": self.mapper.register(name, 1), "name": name, "group_name": "", "other_names": [],
                "is_banned": False, "is_deleted": False, "created_at": boot_ts(), "updated_at": boot_ts(), "urls": []}

    def artists_index(self, params):
        name = (_p(params, "search[name]") or _p(params, "search[any_name_matches]")
                or _p(params, "search[any_name_or_url_matches]") or _p(params, "search[name_like]")
                or _p(params, "search[name_matches]"))
        if not name:
            return Result([], "artists")
        found = self.lookup_tags(name, {1})
        items = sorted(found.items(), key=lambda kv: -kv[1])[: self._limit(params, 20, 1000)]
        return Result(self.only([self._artist(n) for (n, _), _ in items], params), "artists")

    def artists_show(self, artist_id):
        hit = self.mapper.lookup_id(int(artist_id)) if str(artist_id).isdigit() else None
        if not hit or hit[1] != 1:
            raise NotFound()
        return Result(self._artist(hit[0]), "artist")

    # =============================================================== notes
    def notes_index(self, params):
        post_id = _p(params, "search[post_id]") or _p(params, "post_id")
        if post_id:
            ids = [int(x) for x in str(post_id).split(",") if x.strip().isdigit()]
        else:
            q = self.compile("has:notes")
            ids = self.search_ids(q)[: self._limit(params)]
        metas = self.hydrus.file_metadata_known(ids, include_notes=True)
        out = []
        for i in ids:
            if i in metas:
                out.extend(self.conv.notes(metas[i]))
        return Result(self.only(out, params), "notes")

    def notes_show(self, note_id):
        nid = int(note_id)
        meta = self.get_meta(nid // 1000, include_notes=True)
        for n in self.conv.notes(meta):
            if n["id"] == nid:
                return Result(n, "note")
        raise NotFound()

    # =============================================================== favorites
    def _fav_key(self):
        svc = self.hydrus.find_service(self.cfg.favorites_service, {SERVICE_LIKE_RATING})
        if not svc:
            raise ApiError(422, f"hydrus has no like/dislike rating service named "
                                f"'{self.cfg.favorites_service}' (set HYDRUS_FAVORITES_SERVICE)")
        return svc["service_key"]

    def favorites_index(self, params):
        post_id = _p(params, "search[post_id]")
        q = self.compile(f"fav:me{' id:' + str(post_id) if post_id else ''}")
        limit = self._limit(params, 20, 1000)
        ids = self.paginate(self.search_ids(q), q, params, limit)
        return Result([{"id": i, "user_id": 1, "post_id": i} for i in ids], "favorites")

    def favorites_create(self, params, base):
        self._require_writes()
        post_id = _p(params, "post_id") or _p(params, "favorite[post_id]")
        meta = self.get_meta(post_id)
        self.hydrus.set_rating(meta["file_id"], self._fav_key(), True)
        self.search_cache.clear()
        return Result({"id": meta["file_id"], "user_id": 1, "post_id": meta["file_id"]}, "favorite", 201)

    def favorites_delete(self, post_id):
        self._require_writes()
        meta = self.get_meta(post_id)
        self.hydrus.set_rating(meta["file_id"], self._fav_key(), None)
        self.search_cache.clear()
        return Result(None, "favorite", 204)

    def post_vote(self, post_id, params, base):
        """POST /posts/{id}/votes.json?score=1|-1 -> score rating service, if configured."""
        self._require_writes()
        from .hydrus import SERVICE_INCDEC_RATING, SERVICE_NUMERICAL_RATING
        meta = self.get_meta(post_id)
        svc = self.hydrus.find_service(self.cfg.score_service, {SERVICE_NUMERICAL_RATING, SERVICE_INCDEC_RATING})
        if not svc:
            raise ApiError(422, "no score rating service configured (set HYDRUS_SCORE_SERVICE)")
        delta = 1 if str(_p(params, "score", "1")).lstrip("+") in ("1", "up") else -1
        cur = (meta.get("ratings") or {}).get(svc["service_key"]) or 0
        lo = svc.get("min_stars", 0) if svc.get("type") == SERVICE_NUMERICAL_RATING else 0
        hi = svc.get("max_stars", 5) if svc.get("type") == SERVICE_NUMERICAL_RATING else 1 << 30
        new = max(lo, min(hi, int(cur) + delta))
        self.hydrus.set_rating(meta["file_id"], svc["service_key"], new if new > 0 or svc.get("allows_zero") else None)
        self.search_cache.clear()
        return Result({"id": stable_id(f"vote{meta['file_id']}"), "post_id": meta["file_id"], "user_id": 1,
                       "score": delta, "created_at": ts(import_time(meta)), "updated_at": ts(import_time(meta)),
                       "is_deleted": False}, "post-vote", 201)

    def _require_writes(self):
        if not self.cfg.allow_writes:
            raise ApiError(403, "this bridge is read-only (ALLOW_WRITES=false)", "User::PrivilegeError")

    # =============================================================== users
    def profile(self, user):
        return Result(self.conv.user(user) if user else self.conv.anonymous(), "user")

    def users_index(self, params, user):
        name = _p(params, "search[name]") or _p(params, "search[name_matches]")
        u = self.conv.user(user or "hydrus", private=False)
        if name and not fnmatch.fnmatch(u["name"].lower(), name.lower()):
            return Result([], "users")
        return Result([u], "users")

    def users_show(self, uid, user):
        if str(uid) != "1":
            raise NotFound()
        return Result(self.conv.user(user or "hydrus", private=False), "user")

    # =============================================================== files
    def data(self, variant, file_id, sig):
        if not self.conv.verify(variant, file_id, sig):
            raise ApiError(403, "bad file signature", "User::PrivilegeError")
        meta = self.get_meta(file_id)
        fid = meta["file_id"]
        if variant == "original":
            return FileResult("/get_files/file", {"file_id": fid}, 31536000)
        if variant == "180x180" or not is_still(meta):
            return FileResult("/get_files/thumbnail", {"file_id": fid}, 86400)
        box = {"360x360": 360, "720x720": 720}.get(variant)
        w, h = meta.get("width") or 1, meta.get("height") or 1
        if variant == "sample":
            size = self.cfg.sample_size
            rw, rh = size, max(1, round(h * size / w))
            fmt, quality = 1, 85
        elif box:
            s = min(1.0, box / w, box / h)
            rw, rh = max(1, round(w * s)), max(1, round(h * s))
            fmt, quality = (1, 85) if box == 360 else (33, 80)
        else:
            raise NotFound()
        return FileResult("/get_files/render", {"file_id": fid, "render_format": fmt,
                                                "render_quality": quality, "width": rw, "height": rh}, 31536000)

    def status(self):
        try:
            v = self.hydrus.api_version()
            ok = True
        except HydrusError as e:
            v, ok = {"error": e.message}, False
        return Result({"bridge": "hydrus-danbooru-bridge", "hydrus_reachable": ok, "hydrus": v}, "status",
                      200 if ok else 503)
