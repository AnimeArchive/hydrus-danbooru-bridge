"""End-to-end tests: real bridge HTTP server <-> fake hydrus.  Run: python -m unittest -v"""

import base64
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import fake_hydrus  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.query import Compiler, tokenize  # noqa: E402

API_KEY = "secret-key"


def start_bridge(hydrus_port, api_key=""):
    os.environ.update({
        "HYDRUS_URL": f"http://127.0.0.1:{hydrus_port}",
        "HYDRUS_ACCESS_KEY": fake_hydrus.ACCESS_KEY,
        "BRIDGE_API_KEY": api_key, "BRIDGE_LOGIN": "tester" if api_key else "",
        "HYDRUS_SCORE_SERVICE": "stars", "LOG_LEVEL": "WARNING", "SEARCH_CACHE_TTL": "0",
    })
    from bridge import server
    cfg = Config()
    bridge = server.Bridge(cfg)
    handler = type("H", (server.Handler,), {"bridge": bridge, "cfg": cfg})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, bridge


class Base(unittest.TestCase):
    api_key = ""

    @classmethod
    def setUpClass(cls):
        cls.hydrus = fake_hydrus.start()
        cls.httpd, cls.bridge = start_bridge(cls.hydrus.server_port, cls.api_key)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.hydrus.shutdown()

    def req(self, path, params=None, method="GET", data=None, headers=None, raw=False):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        r = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r) as resp:
                content = resp.read()
                return resp.status, (content if raw else json.loads(content) if content else None), resp.headers
        except urllib.error.HTTPError as e:
            content = e.read()
            return e.code, (content if raw else json.loads(content) if content else None), e.headers

    def ids(self, tags, **extra):
        status, body, _ = self.req("/posts.json", dict(tags=tags, **extra))
        self.assertEqual(status, 200, body)
        return [p["id"] for p in body]


class TestSearch(Base):
    def test_default_order_is_id_desc(self):
        self.assertEqual(self.ids(""), [40, 30, 20, 10])

    def test_bare_tag_resolves_namespace(self):
        self.assertEqual(self.ids("hatsune_miku"), [40, 10])
        self.assertEqual(self.ids("vocaloid"), [20, 10])
        self.assertEqual(self.ids("someartist"), [30, 10])

    def test_negation_and_rating(self):
        self.assertEqual(self.ids("blue_eyes -rating:e"), [10])
        self.assertEqual(self.ids("rating:e"), [20])
        self.assertEqual(self.ids("rating:q"), [40, 30])  # unrated -> default rating q
        self.assertEqual(self.ids("is:sfw"), [10])
        self.assertEqual(self.ids("-rating:q"), [20, 10])

    def test_or_syntax(self):
        self.assertEqual(self.ids("~kagamine_rin ~blue_sky"), [30, 20])
        self.assertEqual(self.ids("kagamine_rin or blue_sky"), [30, 20])
        self.assertEqual(self.ids("(kagamine_rin or hatsune_miku) -animated"), [20, 10])
        self.assertEqual(self.ids("-(long_hair or blue_sky)"), [10])

    def test_wildcard(self):
        self.assertEqual(self.ids("*_hair"), [40, 20, 10])
        self.assertEqual(self.ids("kagamine*"), [20])

    def test_metatags(self):
        self.assertEqual(self.ids("width:>1000"), [30, 10])
        self.assertEqual(self.ids("width:640..1500 height:<=1100"), [20, 10])
        self.assertEqual(self.ids("filetype:gif"), [40])
        self.assertEqual(self.ids("-filetype:gif,webm"), [20, 10])
        self.assertEqual(self.ids("id:>15 id:<=30"), [30, 20])
        self.assertEqual(self.ids("id:20,10 order:custom"), [20, 10])
        self.assertEqual(self.ids("date:2024-03-09..2024-07-03"), [30, 20])
        self.assertEqual(self.ids("fav:me"), [10])
        self.assertEqual(self.ids("user:anyone pool:none"), [40, 30, 20, 10])
        self.assertEqual(self.ids("status:pending"), [])
        self.assertEqual(self.ids("has:notes"), [10])

    def test_order_and_pagination(self):
        self.assertEqual(self.ids("order:id"), [10, 20, 30, 40])
        self.assertEqual(self.ids("order:width_asc"), [20, 40, 10, 30])
        self.assertEqual(self.ids("", limit=2, page=2), [20, 10])
        self.assertEqual(self.ids("", limit=2, page="b30"), [20, 10])
        self.assertEqual(self.ids("", limit=2, page="a10"), [30, 20])
        self.assertEqual(self.ids("limit:1"), [40])
        self.assertEqual(self.ids("", page=99), [])

    def test_md5_lookup_returns_single_object(self):
        md5 = fake_hydrus.FILES[20]["_md5"]
        status, body, _ = self.req("/posts.json", {"md5": md5})
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], 20)
        self.assertEqual(self.ids(f"md5:{md5}"), [20])

    def test_counts(self):
        _, body, _ = self.req("/counts/posts.json", {"tags": "long_hair"})
        self.assertEqual(body, {"counts": {"posts": 2}})

    def test_random(self):
        status, body, _ = self.req("/posts/random.json", {"tags": "vocaloid"})
        self.assertEqual(status, 200)
        self.assertIn(body["id"], (10, 20))


class TestPostFormat(Base):
    def test_post_fields(self):
        status, p, _ = self.req("/posts/10.json")
        self.assertEqual(status, 200)
        f = fake_hydrus.FILES[10]
        self.assertEqual(p["md5"], f["_md5"])
        self.assertEqual(p["rating"], "g")
        self.assertEqual(p["tag_string_character"], "hatsune_miku")
        self.assertEqual(p["tag_string_copyright"], "vocaloid")
        self.assertEqual(p["tag_string_artist"], "someartist")
        self.assertEqual(p["tag_string_meta"], "highres")
        self.assertEqual(p["tag_string_general"], "blonde_hair blue_eyes species:cat")
        self.assertEqual(p["tag_count"], 7)
        self.assertEqual(p["source"], "https://www.pixiv.net/artworks/12345")
        self.assertEqual(p["pixiv_id"], 12345)
        self.assertEqual(p["fav_count"], 1)
        self.assertEqual(p["score"], 4)
        self.assertTrue(p["has_large"])
        self.assertEqual([v["type"] for v in p["media_asset"]["variants"]],
                         ["180x180", "360x360", "720x720", "sample", "original"])
        self.assertEqual(p["large_file_url"], p["media_asset"]["variants"][3]["url"])
        self.assertTrue(p["file_url"].endswith(f"{f['_md5']}.jpg"))
        self.assertRegex(p["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$")

    def test_only(self):
        _, body, _ = self.req("/posts.json", {"tags": "id:20", "only": "id,rating,media_asset[variants[type]]"})
        self.assertEqual(body[0]["id"], 20)
        self.assertEqual(set(body[0]), {"id", "rating", "media_asset"})
        self.assertEqual(body[0]["media_asset"]["variants"][0], {"type": "180x180"})

    def test_xml(self):
        status, raw, headers = self.req("/posts.xml", {"tags": "id:20"}, raw=True)
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertIn('<posts type="array">', text)
        self.assertIn('<id type="integer">20</id>', text)
        self.assertIn("<tag-string-character>kagamine_rin</tag-string-character>", text)
        status, raw, _ = self.req("/posts.xml", {"tags": "nonexistent_tag_xyz"}, raw=True)
        self.assertIn('<posts type="array"/>', raw.decode())

    def test_404(self):
        status, body, _ = self.req("/posts/999.json")
        self.assertEqual(status, 404)
        self.assertFalse(body["success"])


class TestFiles(Base):
    def test_original_and_range(self):
        _, p, _ = self.req("/posts/20.json")
        url = p["file_url"].replace(self.base, "")
        status, raw, h = self.req(url, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(raw, fake_hydrus.FILES[20]["_content"])
        self.assertEqual(h["Content-Type"], "image/png")
        status, raw, h = self.req(url, raw=True, headers={"Range": "bytes=0-9"})
        self.assertEqual(status, 206)
        self.assertEqual(raw, fake_hydrus.FILES[20]["_content"][:10])

    def test_sample_uses_render(self):
        _, p, _ = self.req("/posts/10.json")
        status, raw, _ = self.req(p["large_file_url"].replace(self.base, ""), raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"/get_files/render", raw)
        self.assertIn(b'"width": "850"', raw)

    def test_video_preview_uses_thumbnail(self):
        _, p, _ = self.req("/posts/30.json")
        self.assertFalse(p["has_large"])
        status, raw, _ = self.req(p["media_asset"]["variants"][2]["url"].replace(self.base, ""), raw=True)
        self.assertIn(b"/get_files/thumbnail", raw)

    def test_bad_signature_rejected_only_with_key(self):
        status, _, _ = self.req("/data/original/10/p/x.jpg", raw=True)
        self.assertEqual(status, 200)  # no BRIDGE_API_KEY -> "p" signature is valid


class TestTags(Base):
    def test_tags_index(self):
        _, body, _ = self.req("/tags.json", {"search[name_matches]": "*hair", "search[order]": "count"})
        self.assertEqual([t["name"] for t in body], ["long_hair", "blonde_hair"])
        self.assertEqual(body[0]["post_count"], 2)
        _, body, _ = self.req("/tags.json", {"search[name]": "hatsune_miku"})
        self.assertEqual(body[0]["category"], 4)
        status, tag, _ = self.req(f"/tags/{body[0]['id']}.json")
        self.assertEqual(tag["name"], "hatsune_miku")
        _, body, _ = self.req("/tags.json", {"search[name_matches]": "*", "search[category]": "1"})
        self.assertEqual([t["name"] for t in body], ["someartist"])

    def test_autocomplete(self):
        _, body, _ = self.req("/autocomplete.json", {"search[query]": "blue", "search[type]": "tag_query"})
        self.assertEqual([t["value"] for t in body], ["blue_eyes", "blue_sky"])
        self.assertEqual(body[0]["post_count"], 2)
        _, body, _ = self.req("/autocomplete.json", {"search[query]": "order:wi", "search[type]": "tag_query"})
        self.assertEqual(body[0], {"type": "static", "label": "width", "value": "order:width"})
        _, body, _ = self.req("/tags/autocomplete.json", {"search[name_matches]": "hats"})
        self.assertEqual(body[0]["name"], "hatsune_miku")

    def test_related(self):
        _, body, _ = self.req("/related_tag.json", {"query": "hatsune_miku"})
        self.assertEqual(body["post_count"], 2)
        names = {r["tag"]["name"]: r["frequency"] for r in body["related_tags"]}
        self.assertEqual(names["hatsune_miku"], 1.0)
        self.assertEqual(names["long_hair"], 0.5)

    def test_aliases_implications(self):
        _, body, _ = self.req("/tag_aliases.json", {"search[consequent_name]": "hatsune_miku"})
        self.assertEqual([a["antecedent_name"] for a in body], ["miku"])
        _, body, _ = self.req("/tag_implications.json", {"search[antecedent_name]": "hatsune_miku"})
        self.assertEqual([(a["antecedent_name"], a["consequent_name"]) for a in body], [("hatsune_miku", "vocaloid")])

    def test_artists_notes_profile(self):
        _, body, _ = self.req("/artists.json", {"search[name]": "someartist"})
        self.assertEqual(body[0]["name"], "someartist")
        _, body, _ = self.req("/notes.json", {"search[post_id]": "10"})
        self.assertIn("hello", body[0]["body"])
        _, body, _ = self.req("/profile.json")
        self.assertEqual(body["id"], 1)
        _, body, _ = self.req("/pools.json")
        self.assertEqual(body, [])


class TestWrites(Base):
    def test_favorite_roundtrip(self):
        status, _, _ = self.req("/favorites.json", data={"post_id": "20"}, method="POST")
        self.assertEqual(status, 201)
        self.assertEqual(self.ids("fav:me"), [20, 10])
        status, _, _ = self.req("/favorites/20.json", method="DELETE")
        self.assertEqual(status, 204)
        self.assertEqual(self.ids("fav:me"), [10])

    def test_tag_edit(self):
        status, p, _ = self.req("/posts/20.json", method="PUT", data={
            "post[tag_string]": "blue_eyes character:kagamine_rin vocaloid new_tag artist:newartist rating:q"})
        self.assertEqual(status, 200, p)
        self.assertEqual(p["rating"], "q")
        self.assertEqual(p["tag_string_general"], "blue_eyes new_tag")
        self.assertEqual(p["tag_string_artist"], "newartist")
        tags = fake_hydrus.FILES[20]["tags"][fake_hydrus.MY_TAGS]["storage_tags"]["0"]
        self.assertIn("creator:newartist", tags)
        self.assertIn("rating:questionable", tags)
        self.assertNotIn("long hair", tags)
        self.assertNotIn("rating:explicit", tags)


class TestAuth(Base):
    api_key = API_KEY

    def test_requires_key(self):
        status, body, _ = self.req("/posts.json")
        self.assertEqual(status, 401)
        status, _, _ = self.req("/posts.json", {"login": "tester", "api_key": "wrong"})
        self.assertEqual(status, 401)
        status, body, _ = self.req("/posts.json", {"login": "tester", "api_key": API_KEY})
        self.assertEqual(status, 200)
        basic = base64.b64encode(f"tester:{API_KEY}".encode()).decode()
        status, body, _ = self.req("/posts/10.json", headers={"Authorization": "Basic " + basic})
        self.assertEqual(status, 200)
        # signed file url works without credentials; tampered one does not
        s, _, _ = self.req(body["file_url"].replace(self.base, ""), raw=True)
        self.assertEqual(s, 200)
        s, _, _ = self.req(body["file_url"].replace(self.base, "").replace("/10/", "/20/"), raw=True)
        self.assertEqual(s, 403)
        status, body, _ = self.req("/profile.json")
        self.assertEqual(body["name"], "Anonymous")


class TestCompiler(unittest.TestCase):
    def test_tokenize_parens(self):
        self.assertEqual(tokenize("(a b) or -(c_(d) e)"), ["(", "a", "b", ")", "or", "-", "(", "c_(d)", "e", ")"])
        self.assertEqual(tokenize("(^_^) hatsune_miku_(cosplay)"), ["(^_^)", "hatsune_miku_(cosplay)"])
        self.assertEqual(tokenize("miku_(cosplay) source:\"a b\""), ["miku_(cosplay)", "source:a b"])

    def test_system_predicates(self):
        cfg = Config()

        class FakeMapper:
            def resolve(self, name):
                return [name.replace("_", " ")]

            def rating_tags(self, l):
                return {"g": ["rating:general", "rating:safe"]}.get(l, [f"rating:{l}"])

        class FakeHydrus:
            def find_service(self, *a):
                return None

        c = Compiler(cfg, FakeHydrus(), FakeMapper())
        self.assertEqual(c.compile("filesize:>=1M").tags, ["system:filesize > 1048575 B"])
        self.assertEqual(c.compile("width:>=100").tags, ["system:width >= 100"])
        self.assertEqual(c.compile("tagcount:>=5").tags, ["system:number of tags > 4"])
        self.assertEqual(c.compile("age:<2w").tags, ["system:time imported < 14 days 0 hours"])
        self.assertEqual(c.compile("ratio:16:9").tags, ["system:ratio = 16:9"])
        self.assertEqual(c.compile("mpixels:>2").tags, ["system:num pixels > 2000000 px"])
        self.assertEqual(c.compile("duration:>5").tags, ["system:duration > 5000 milliseconds"])
        self.assertEqual(c.compile("source:none").tags, ["system:no urls"])
        self.assertEqual(c.compile("-width:>100").tags, ["system:width <= 100"])
        self.assertEqual(c.compile("a -b").tags, ["a", "-b"])
        self.assertEqual(c.compile("a ~b ~c").tags, ["a", ["b", "c"]])
        self.assertEqual(c.compile("(a b) or c").tags, [["a", "c"], ["b", "c"]])
        self.assertEqual(c.compile("rating:g").tags, [["rating:general", "rating:safe"]])
        self.assertEqual(c.compile("").tags, ["system:everything"])
        self.assertTrue(c.compile("is:pending").impossible)
        q = c.compile("order:score_asc limit:5")
        self.assertEqual((q.sort_type, q.sort_asc, q.limit), (10, True, 5))


if __name__ == "__main__":
    unittest.main()
