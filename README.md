# hydrus-danbooru-bridge

A small self-hosted server that **speaks the Danbooru API** and answers from your
**Hydrus Network client** (via the Hydrus Client API). Point any Danbooru client
(Grabber, gallery-dl, booru browser apps, Tachiyomi-style extensions, scripts) at it
and browse, search and download your Hydrus library as if it were a Danbooru.

- Pure Python standard library, no dependencies. Ships as a ~70 MB Alpine image.
- Danbooru requests are translated to Hydrus calls; Hydrus responses are translated
  back into Danbooru-shaped JSON **and** XML (Rails-style, as Danbooru emits).
- Files, thumbnails and samples are streamed through the bridge (with HTTP Range
  support for video seeking), so your Hydrus access key is never exposed.

## Quick start (Docker)

1. In Hydrus: *services → manage services* → enable the **client api**.
   Then *services → review services → client api → add → manually* and create a key with:
   - **search for and fetch files** (required)
   - **edit file tags** (required for tag autocomplete, and for tag edits)
   - **edit file ratings** (favorites / votes)
   - **import and edit urls** (setting a post's source)
2. Configure and start. The prebuilt image is published to GitHub Container Registry
   for `linux/amd64` and `linux/arm64`:
   ```bash
   cp .env.example .env        # set HYDRUS_ACCESS_KEY and BRIDGE_API_KEY
   docker compose up -d        # pulls ghcr.io/animearchive/hydrus-danbooru-bridge:latest
   ```
   Or without compose:
   ```bash
   docker run -d --name hydrus-danbooru-bridge -p 8000:8000 --env-file .env \
     --add-host host.docker.internal:host-gateway \
     ghcr.io/animearchive/hydrus-danbooru-bridge:latest
   ```
   Update with `docker compose pull && docker compose up -d`. Image tags: `latest` (main
   branch), `X.Y.Z` / `X.Y` (release tags `vX.Y.Z`), and `sha-<commit>`. To build from source
   instead, use `build: .` in `docker-compose.yml`.
3. In your client, add a **Danbooru (2.0)** site with URL `http://<host>:8000`,
   username = `BRIDGE_LOGIN`, API key = `BRIDGE_API_KEY`.

`HYDRUS_URL` defaults to `http://host.docker.internal:45869` (the Hydrus client running
on the Docker host). The compose file maps `host.docker.internal` on Linux too. If Hydrus
runs elsewhere, set its address. In Hydrus, the client API must accept non-local
connections if the container reaches it over a bridge network ("allow non-local
connections" in the client api service settings).

Without Docker: `python -m bridge` (Python 3.9+), configured by the same environment variables.

## How it maps

| Danbooru | Hydrus |
|---|---|
| post `id` | `file_id` |
| `md5` | via `/get_files/file_hashes` (Hydrus stores md5 for every import) |
| `tag_string_artist` | `creator:` (also `artist:`, `studio:`) |
| `tag_string_copyright` | `series:` (also `copyright:`, `franchise:`) |
| `tag_string_character` | `character:` (also `person:`) |
| `tag_string_meta` | `meta:` (also `medium:`) |
| `tag_string_general` | unnamespaced tags; other namespaces keep their prefix (`species:cat`) |
| `rating` g/s/q/e | `rating:` tags (`safe`/`general`, `sensitive`, `questionable`, `explicit`); untagged → `DEFAULT_RATING` |
| `source`, `pixiv_id` | known URLs (post pages preferred over direct file links) |
| `fav_count`, favorites | like/dislike rating service `favourites` (`HYDRUS_FAVORITES_SERVICE`) |
| `score` | optional numerical or inc/dec rating service (`HYDRUS_SCORE_SERVICE`) |
| `created_at` | import time; `updated_at` = modified/archived time |
| notes | Hydrus notes (name + text; placed at the top-left since Hydrus notes have no box) |
| tag aliases / implications | Hydrus siblings / parents |
| `preview_file_url` (180x180) | `/get_files/thumbnail` |
| `360x360`, `720x720`, `large_file_url` (sample, 850px) | `/get_files/render` for still images; the thumbnail for video/animation |
| `file_url` | `/get_files/file` (Range supported) |

Tags are shown Danbooru-style (spaces → underscores). When you search a bare tag like
`hatsune_miku`, the bridge looks it up with Hydrus autocomplete and searches every
Hydrus tag that maps to it (for example `character:hatsune miku`).

## Endpoints

`/posts.json` (`tags`, `limit`, `page` incl. `b<id>`/`a<id>`, `random`, `md5`, `only`),
`/posts/{id}.json`, `/posts/random.json`, `PUT /posts/{id}.json` (tag/rating/source edits),
`POST /posts/{id}/votes.json`, `/counts/posts.json`, `/explore/posts/popular.json`,
`/tags.json` (`search[name]`, `search[name_matches]`, `search[fuzzy_name_matches]`,
`search[category]`, `search[order]`, `search[hide_empty]`), `/tags/{id}.json`,
`/tags/autocomplete.json` (legacy), `/autocomplete.json` (`tag_query`, `tag`, `artist`),
`/related_tag.json`, `/tag_aliases.json`, `/tag_implications.json`, `/artists.json`,
`/notes.json`, `/favorites.json` (GET/POST/DELETE), `/profile.json`, `/users.json`,
`/healthz`.

Every route also answers as `.xml`. Danbooru features Hydrus has no equivalent for
(pools, wiki, comments, forum, versions) return empty lists, so clients don't break.

## Search syntax

Supported: AND, `-negation`, `~a ~b`, `a or b`, `( … )` groups, `*` wildcards, and these metatags:

| Metatag | Hydrus predicate |
|---|---|
| `rating:g,s` `is:sfw` `is:nsfw` | rating tags |
| `order:` `id`, `id_desc`, `created_at[_asc]`, `change`, `filesize`, `mpixels`, `landscape`, `portrait`, `tagcount`, `duration`, `md5`, `random`, `custom`, `score` / `favcount` / `rank` (→ view count) | `file_sort_type`, or sorted in the bridge |
| Hydrus-only orders: `order:views`, `viewtime`, `width`, `height`, `framerate`, `frames`, `archived`, `last_viewed`, `bitrate`, `hue`, `lightness` | `file_sort_type` |
| `width:` `height:` `filesize:` `duration:` `mpixels:` `ratio:` `tagcount:` `gentags:` `arttags:` `chartags:` `copytags:` `metatags:` `notes:` | the matching `system:` predicate, with full range syntax `>`, `>=`, `..`, `...`, `a,b,c` |
| `date:` `age:` | `system:time imported` |
| `id:` | exact ids use `system:hash`; ranges are filtered in the bridge |
| `md5:` `filetype:` `is:png` `source:` `source:none` `has:source` `has:notes` `pixiv:` `fav:` `ordfav:` `favcount:` `score:` `limit:` `random:` `status:deleted` (trash) | the matching `system:` predicate |
| extras: `is:inbox`, `is:archive`, `has:audio` | the matching `system:` predicate |
| raw passthrough, e.g. `system:has_audio`, `system:width_>_1000` | underscores become spaces |

Metatags for things Hydrus doesn't have (`parent:`, `pool:`, `commenter:`, `status:pending`,
and so on) behave as they would on a Danbooru where those things never happen:
`pool:none` matches everything and `pool:x` matches nothing.

## Configuration

See [.env.example](.env.example) for all options. The important ones:

| Variable | Default | |
|---|---|---|
| `HYDRUS_URL` | `http://127.0.0.1:45869` | Hydrus Client API address (the compose `.env` uses `host.docker.internal`) |
| `HYDRUS_ACCESS_KEY` | — | **required** |
| `BRIDGE_API_KEY` / `BRIDGE_LOGIN` | empty | enables auth (query `login`+`api_key`, or HTTP Basic) |
| `BRIDGE_PUBLIC_URL` | derived from `Host` / `X-Forwarded-*` | base for `file_url` etc. Set it behind a reverse proxy |
| `ALLOW_WRITES` | `true` | `false` makes the bridge read-only |
| `HYDRUS_TAG_SERVICE_KEY` | all known tags | tag service used for search and display |
| `HYDRUS_WRITE_TAG_SERVICE_KEY` | my tags | where Danbooru tag edits are written |
| `NAMESPACE_MAP` | see above | JSON `{"namespace": category}` overrides |

**Security:** when `BRIDGE_API_KEY` is set, every API call needs credentials. File URLs are
HMAC-signed with that key, so image loaders can fetch them without credentials but can't
guess other files' URLs. When it's not set, anyone who can reach the port can read your
library.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests start the real bridge against a fake Hydrus Client API ([tests/fake_hydrus.py](tests/fake_hydrus.py))
and cover search translation, pagination, post format, XML, file proxying with Range
requests, tags, autocomplete, aliases, favorites, tag edits and auth.
