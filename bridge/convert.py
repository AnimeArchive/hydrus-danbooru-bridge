"""Hydrus objects -> Danbooru JSON/XML objects."""

import datetime
import hashlib
import hmac
import re
from xml.sax.saxutils import escape

from .config import CAT_ARTIST, CAT_CHARACTER, CAT_COPYRIGHT, CAT_GENERAL, CAT_META
from .hydrus import KEY_ALL_LOCAL_FILES, KEY_COMBINED_LOCAL_MEDIA
from .tags import words

STILL_MIMES = {
    "image/jpeg", "image/png", "image/webp", "image/bmp", "image/avif", "image/heif",
    "image/heic", "image/jxl", "image/tiff", "image/x-icon", "image/vnd.microsoft.icon",
    "image/vnd.adobe.photoshop", "image/qoi",
}
BOORU_DOMAINS = ("donmai.us", "gelbooru", "safebooru", "yande.re", "konachan", "sankaku",
                 "e621", "e926", "rule34", "booru", "zerochan", "anime-pictures")
FILE_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif|mp4|webm|zip|jxl|bmp)(\?|$)", re.I)
PIXIV_RE = re.compile(r"pixiv\.net/.*(?:artworks/|illust_id=)(\d+)")

BOOT_TIME = datetime.datetime.now().astimezone()


def ts(t):
    if t is None:
        return None
    try:
        dt = datetime.datetime.fromtimestamp(float(t)).astimezone()
    except (OSError, OverflowError, ValueError):  # e.g. pre-1970 on Windows
        dt = datetime.datetime.fromtimestamp(max(float(t), 86400), datetime.timezone.utc)
    return dt.isoformat(timespec="milliseconds")


def now_ts():
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def boot_ts():
    return BOOT_TIME.isoformat(timespec="milliseconds")


def fit(w, h, box):
    if not w or not h:
        return box, box
    if w <= box and h <= box:
        return w, h
    s = min(box / w, box / h)
    return max(1, round(w * s)), max(1, round(h * s))


def is_still(meta):
    return meta.get("mime") in STILL_MIMES and (meta.get("num_frames") or 1) <= 1


def import_time(meta):
    fs = meta.get("file_services") or {}
    cur = fs.get("current") or {}
    for key in (KEY_COMBINED_LOCAL_MEDIA, KEY_ALL_LOCAL_FILES):
        t = (cur.get(key) or {}).get("time_imported")
        if t:
            return t
    times = [v.get("time_imported") for d in (cur, fs.get("deleted") or {}) for v in d.values()
             if isinstance(v, dict) and v.get("time_imported")]
    if times:
        return min(times)
    return meta.get("time_modified") or 0


def pick_source(urls):
    def rank(u):
        low = u.lower()
        if FILE_EXT_RE.search(low):
            return 2
        if any(d in low for d in BOORU_DOMAINS):
            return 1
        return 0
    return min(urls, key=rank) if urls else ""


class Converter:
    def __init__(self, cfg, hydrus, mapper):
        self.cfg, self.hydrus, self.mapper = cfg, hydrus, mapper

    # ------------------------------------------------------------- signing
    def sign(self, variant, file_id):
        if not self.cfg.bridge_api_key:
            return "p"
        msg = f"{variant}:{file_id}".encode()
        return hmac.new(self.cfg.bridge_api_key.encode(), msg, hashlib.sha256).hexdigest()[:20]

    def verify(self, variant, file_id, sig):
        return hmac.compare_digest(self.sign(variant, file_id), sig)

    def data_url(self, base, variant, meta, name, ext):
        return f"{base}/data/{variant}/{meta['file_id']}/{self.sign(variant, meta['file_id'])}/{name}.{ext}"

    # ------------------------------------------------------------- services
    def _service_key(self, name, types):
        svc = self.hydrus.find_service(name, types) if name else None
        return svc["service_key"] if svc else None

    def rating_keys(self):
        from .hydrus import SERVICE_INCDEC_RATING, SERVICE_LIKE_RATING, SERVICE_NUMERICAL_RATING
        try:
            fav = self._service_key(self.cfg.favorites_service, {SERVICE_LIKE_RATING})
            score = self._service_key(self.cfg.score_service, {SERVICE_NUMERICAL_RATING, SERVICE_INCDEC_RATING})
        except Exception:
            fav = score = None
        return fav, score

    # ------------------------------------------------------------- tags
    def file_tags(self, meta):
        """-> ({category: sorted names}, rating_letter)"""
        tags = (meta.get("tags") or {}).get(self.cfg.tag_service_key or "616c6c206b6e6f776e2074616773") or {}
        disp = tags.get("display_tags") or {}
        raw = list(disp.get("0", [])) + list(disp.get("1", []))
        cats = {CAT_GENERAL: set(), CAT_ARTIST: set(), CAT_COPYRIGHT: set(), CAT_CHARACTER: set(), CAT_META: set()}
        rating = None
        for t in raw:
            name, cat = self.mapper.classify(t)
            if name is None:
                rating = rating or cat[1]
                continue
            if name:
                cats.setdefault(cat, set()).add(name)
        return {c: sorted(v) for c, v in cats.items()}, rating or self.cfg.default_rating

    # ------------------------------------------------------------- posts
    def post(self, meta, base, md5s, rating_keys=None):
        fid = meta["file_id"]
        sha = meta.get("hash", "")
        md5 = md5s.get(sha) or sha
        cats, rating = self.file_tags(meta)
        fav_key, score_key = rating_keys or self.rating_keys()
        ratings = meta.get("ratings") or {}
        score = ratings.get(score_key) if score_key else 0
        score = score if isinstance(score, int) and not isinstance(score, bool) else 0
        fav_count = 1 if fav_key and ratings.get(fav_key) is True else 0

        w, h = meta.get("width") or 0, meta.get("height") or 0
        ext = (meta.get("ext") or "").lstrip(".") or "bin"
        created = import_time(meta)
        updated = max(x for x in (created, meta.get("time_modified") or 0, meta.get("time_archived") or 0))
        urls = meta.get("known_urls") or []
        pixiv = next((int(m.group(1)) for u in urls for m in [PIXIV_RE.search(u)] if m), None)
        still = is_still(meta)

        variants = []
        tw, th = fit(w, h, 180)
        variants.append({"type": "180x180", "url": self.data_url(base, "180x180", meta, md5, "jpg"),
                         "width": tw, "height": th, "file_ext": "jpg"})
        for box, vext in ((360, "jpg"), (720, "webp")):
            vw, vh = fit(w, h, box)
            if not still:
                vw, vh, vext = tw, th, "jpg"
            variants.append({"type": f"{box}x{box}", "url": self.data_url(base, f"{box}x{box}", meta, md5, vext),
                             "width": vw, "height": vh, "file_ext": vext})
        has_large = bool(still and self.cfg.sample_size and w > self.cfg.sample_size)
        if has_large:
            sh = max(1, round(h * self.cfg.sample_size / w))
            variants.append({"type": "sample", "url": self.data_url(base, "sample", meta, "sample-" + md5, "jpg"),
                             "width": self.cfg.sample_size, "height": sh, "file_ext": "jpg"})
        original = self.data_url(base, "original", meta, md5, ext)
        variants.append({"type": "original", "url": original, "width": w, "height": h, "file_ext": ext})

        all_tags = sorted(set().union(*cats.values()))
        duration = meta.get("duration")
        return {
            "id": fid,
            "created_at": ts(created),
            "uploader_id": 1,
            "score": score,
            "source": pick_source(urls),
            "md5": md5,
            "last_comment_bumped_at": None,
            "rating": rating,
            "image_width": w,
            "image_height": h,
            "tag_string": " ".join(all_tags),
            "fav_count": fav_count,
            "file_ext": ext,
            "last_noted_at": None,
            "parent_id": None,
            "has_children": False,
            "approver_id": None,
            "tag_count_general": len(cats[CAT_GENERAL]),
            "tag_count_artist": len(cats[CAT_ARTIST]),
            "tag_count_character": len(cats[CAT_CHARACTER]),
            "tag_count_copyright": len(cats[CAT_COPYRIGHT]),
            "file_size": meta.get("size") or 0,
            "up_score": max(score, 0),
            "down_score": min(score, 0),
            "is_pending": False,
            "is_flagged": False,
            "is_deleted": bool(meta.get("is_trashed") or meta.get("is_deleted")),
            "tag_count": len(all_tags),
            "updated_at": ts(updated),
            "is_banned": False,
            "pixiv_id": pixiv,
            "last_commented_at": None,
            "has_active_children": False,
            "bit_flags": 0,
            "tag_count_meta": len(cats[CAT_META]),
            "has_large": has_large,
            "has_visible_children": False,
            "media_asset": {
                "id": fid,
                "created_at": ts(created),
                "updated_at": ts(updated),
                "md5": md5,
                "file_ext": ext,
                "file_size": meta.get("size") or 0,
                "image_width": w,
                "image_height": h,
                "duration": round(duration / 1000, 3) if duration else None,
                "status": "active",
                "file_key": sha[:9],
                "is_public": True,
                "pixel_hash": meta.get("pixel_hash") or md5,
                "variants": variants,
            },
            "tag_string_general": " ".join(cats[CAT_GENERAL]),
            "tag_string_character": " ".join(cats[CAT_CHARACTER]),
            "tag_string_copyright": " ".join(cats[CAT_COPYRIGHT]),
            "tag_string_artist": " ".join(cats[CAT_ARTIST]),
            "tag_string_meta": " ".join(cats[CAT_META]),
            "file_url": original,
            "large_file_url": variants[-2]["url"] if has_large else original,
            "preview_file_url": variants[0]["url"],
        }

    def posts(self, metas, base):
        md5s = self.hydrus.md5s([m["hash"] for m in metas if m.get("hash")]) if self.cfg.fetch_md5 else {}
        keys = self.rating_keys()
        return [self.post(m, base, md5s, keys) for m in metas]

    # ------------------------------------------------------------- tags
    def tag(self, name, category, post_count):
        return {
            "id": self.mapper.register(name, category),
            "name": name,
            "post_count": post_count,
            "category": category,
            "created_at": boot_ts(),
            "updated_at": boot_ts(),
            "is_deprecated": False,
            "words": words(name),
        }

    def aggregate_tags(self, hydrus_tags):
        """[{value, count}] -> {(name, category): count}; merges sibling spellings."""
        out = {}
        for t in hydrus_tags:
            name, cat = self.mapper.classify(t["value"])
            if not name:
                continue
            key = (name, cat)
            out[key] = max(out.get(key, 0), t.get("count", 0))
        return out

    # ------------------------------------------------------------- notes
    def notes(self, meta):
        notes = meta.get("notes") or {}
        w, h = meta.get("width") or 200, meta.get("height") or 200
        out = []
        for i, (title, body) in enumerate(sorted(notes.items())):
            out.append({
                "id": meta["file_id"] * 1000 + i + 1,
                "created_at": ts(import_time(meta)),
                "updated_at": ts(meta.get("time_modified") or import_time(meta)),
                "x": 0, "y": min(i * 40, max(h - 40, 0)),
                "width": min(w, 300), "height": min(h, 40),
                "is_active": True,
                "post_id": meta["file_id"],
                "body": body if title in ("", "notes", "note") else f"<b>{escape(title)}</b>\n{body}",
                "version": 1,
            })
        return out

    # ------------------------------------------------------------- users
    def user(self, name, private=True):
        u = {
            "id": 1, "created_at": boot_ts(), "name": name, "inviter_id": None, "level": 50,
            "post_upload_count": 0, "post_update_count": 0, "note_update_count": 0,
            "is_deleted": False, "level_string": "Admin", "is_banned": False,
            "wiki_page_version_count": 0, "artist_version_count": 0,
            "artist_commentary_version_count": 0, "pool_version_count": 0,
            "forum_post_count": 0, "comment_count": 0, "favorite_group_count": 0,
            "appeal_count": 0, "flag_count": 0, "positive_feedback_count": 0,
            "neutral_feedback_count": 0, "negative_feedback_count": 0,
        }
        if private:
            u.update({
                "last_logged_in_at": now_ts(), "last_forum_read_at": None, "comment_threshold": -8,
                "default_image_size": "large", "favorite_tags": None, "blacklisted_tags": "",
                "time_zone": "UTC", "per_page": self.cfg.default_limit, "custom_style": "",
                "favorite_count": 0, "theme": "auto", "receive_email_notifications": False,
                "new_post_navigation_layout": True, "enable_private_favorites": False,
                "show_deleted_children": False, "disable_categorized_saved_searches": False,
                "disable_tagged_filenames": False, "disable_mobile_gestures": False,
                "enable_safe_mode": False, "enable_desktop_mode": False,
                "disable_post_tooltips": False, "requires_verification": False,
                "is_verified": True, "show_deleted_posts": False, "statement_timeout": 60000,
                "favorite_group_limit": 0, "tag_query_limit": 1000, "max_saved_searches": 0,
                "api_regen_multiplier": 1.0, "api_burst_limit": 1000,
                "remaining_api_limit": 1000,
            })
        return u

    @staticmethod
    def anonymous():
        return {"id": None, "name": "Anonymous", "level": 0, "level_string": None,
                "created_at": boot_ts(), "blacklisted_tags": "", "per_page": 20,
                "tag_query_limit": 1000, "favorite_count": 0}


# ================================================================= only=
def parse_only(spec):
    """"id,media_asset[variants[url]]" -> {"id": True, "media_asset": {"variants": {"url": True}}}"""
    out, stack, cur = {}, [], ""
    node = out
    for ch in spec:
        if ch == ",":
            if cur.strip():
                node.setdefault(cur.strip(), True)
            cur = ""
        elif ch == "[":
            child = {}
            node[cur.strip()] = child
            stack.append(node)
            node, cur = child, ""
        elif ch == "]":
            if cur.strip():
                node.setdefault(cur.strip(), True)
            cur = ""
            node = stack.pop() if stack else out
        else:
            cur += ch
    if cur.strip():
        node.setdefault(cur.strip(), True)
    return out


def apply_only(obj, spec):
    if isinstance(obj, list):
        return [apply_only(o, spec) for o in obj]
    if not isinstance(obj, dict) or spec is True:
        return obj
    out = {}
    for k, sub in spec.items():
        if k in obj:
            out[k] = apply_only(obj[k], sub)
    return out


# ================================================================= XML
_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?[+-]\d\d:\d\d$")


def _singular(name):
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s"):
        return name[:-1]
    return name + "-item"


def _xml(name, value, out, indent):
    tag = name.replace("_", "-")
    pad = "  " * indent
    if value is None:
        out.append(f'{pad}<{tag} nil="true"/>')
    elif isinstance(value, bool):
        out.append(f'{pad}<{tag} type="boolean">{"true" if value else "false"}</{tag}>')
    elif isinstance(value, int):
        out.append(f'{pad}<{tag} type="integer">{value}</{tag}>')
    elif isinstance(value, float):
        out.append(f'{pad}<{tag} type="float">{value}</{tag}>')
    elif isinstance(value, dict):
        out.append(f"{pad}<{tag}>")
        for k, v in value.items():
            _xml(k, v, out, indent + 1)
        out.append(f"{pad}</{tag}>")
    elif isinstance(value, list):
        if not value:
            out.append(f'{pad}<{tag} type="array"/>')
            return
        out.append(f'{pad}<{tag} type="array">')
        child = _singular(name)
        for v in value:
            _xml(child, v, out, indent + 1)
        out.append(f"{pad}</{tag}>")
    else:
        s = str(value)
        if _TS_RE.match(s):
            s = re.sub(r"\.\d+", "", s)
            out.append(f'{pad}<{tag} type="dateTime">{s}</{tag}>')
        else:
            out.append(f"{pad}<{tag}>{escape(s)}</{tag}>")


def to_xml(root, value):
    out = ['<?xml version="1.0" encoding="UTF-8"?>']
    _xml(root, value, out, 0)
    return "\n".join(out) + "\n"
