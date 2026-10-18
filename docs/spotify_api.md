# Spotify Web API: what this project can and cannot do

Researched 2026-10-17 against:

- Web API docs: <https://developer.spotify.com/documentation/web-api>
- February 2026 migration guide: <https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide>
- OpenAPI schema: <https://developer.spotify.com/reference/web-api/open-api-schema.yaml>
- Developer Policy: <https://developer.spotify.com/policy>
- Developer Terms: <https://developer.spotify.com/terms>
- PKCE tutorial, redirect URI rules, rate limits pages under /documentation/web-api/

Re-check these before relying on anything below; Spotify has changed the API repeatedly.

## Access mode

New apps start in **Development Mode**. Since the February 2026 changes:

- The app owner must have an active **Premium** subscription, or the app stops working.
- Up to **5 users** per app; other accounts must be added in the dashboard's User Management.
- Lower rate limits than Extended Quota Mode, measured over a rolling 30-second window.

Extended Quota Mode is for apps with large user bases and is not relevant to a personal tool.

## Authentication

Authorization Code with **PKCE** (`code_challenge_method=S256`), so no client secret exists in
the project. Redirect URIs must match the dashboard exactly; `localhost` is not allowed, and the
loopback literal `http://127.0.0.1:<port>/...` is. Refresh responses may omit `refresh_token`;
the previous one is then kept (implemented in `TokenSet.from_response`).

Scopes requested in Phase 1 (read-only):

| Scope | Used for |
|---|---|
| `user-library-read` | `GET /me/tracks` (Liked Songs) |
| `playlist-read-private` | `GET /me/playlists`, `GET /playlists/{id}/items` |
| `playlist-read-collaborative` | collaborative playlists |

Phase 6 adds `playlist-modify-private` and `playlist-modify-public` and will require logging in
again. No scope that can modify Liked Songs is ever requested.

## Endpoints used

| Endpoint | Phase | Notes |
|---|---|---|
| `GET /me` | 1 | `country`, `email`, `product`, `followers` no longer returned |
| `GET /me/tracks` | 1 | limit ≤ 50; we pass `market=from_token` |
| `GET /me/playlists` | 1 | limit ≤ 50; playlist object's track ref is now `items` (was `tracks`) |
| `GET /playlists/{id}/items` | 1 | limit ≤ 50; **403 unless you own or collaborate**; entry field `item` (old `track` deprecated) |
| `POST /me/playlists` | 6 | replaces `POST /users/{id}/playlists`; `public` defaults to true, so we always send `false` |
| `PUT /playlists/{id}` | 6 | name, description, public |
| `POST /playlists/{id}/items` | 6 | ≤ 100 URIs per request |
| `PUT /playlists/{id}/items` | 6 | replace (≤ 100) or reorder; we replace first 100 then append |
| `DELETE /playlists/{id}/items` | 6 | body key `items` (was `tracks`), ≤ 100, optional `snapshot_id` |

The write methods exist in `SpotifyClient` and are unit-tested, but nothing calls them yet.

## Unavailable or removed (do not build on these)

- **Audio Features, Audio Analysis, Recommendations**: not available to new apps.
- Batch `GET /tracks?ids=`, `/albums`, `/artists`: removed for Dev Mode; single-ID endpoints only.
- `GET /artists/{id}/top-tracks`, browse/categories/new-releases, `GET /users/{id}`,
  `GET /users/{id}/playlists`, `GET /markets`.
- Track `popularity`, `available_markets`, `linked_from`; album `label`, `popularity`;
  artist `followers`, `popularity` (removed for Dev Mode apps).
- `GET /search` limit dropped to 10 (and we don't use search anyway: no new music).

`external_ids` (ISRC) was removed in February and **restored in March 2026**. It's the main
key for recording-level deduplication and for independent metadata lookups.

## Contents of playlists you don't own

Followed playlists created by other users or by Spotify return metadata only; requesting
their items returns 403. The importer records these as `contents_status='unavailable'` and
caches that by `snapshot_id` so it doesn't retry every run. The inventory shows how many.

## Local files

Playlist entries for local files have `is_local: true`, `id: null`, and a URI that (by
observation, not documentation) looks like
`spotify:local:{artist}:{album}:{title}:{seconds}` with `+`-encoded fields. The parser reads
name/artist/album from the object first and only falls back to the URI. Local files cannot be
added to playlists via the API; `SpotifyClient` raises if asked to.

Only local files placed in a regular playlist are visible. The desktop app's built-in *Local
Files* source is not a playlist and is not exposed by the API.

## Rate limiting

429 responses include `Retry-After` (seconds). The client waits that long if ≤ 120 s
(configurable) and otherwise stops with a `RateLimitedError` rather than hanging for hours.
5xx and network errors retry with exponential backoff + jitter (5 attempts). 401 triggers one
token refresh. Spotify recommends comparing `snapshot_id` to avoid re-downloading playlists;
the importer does this.

## Policy and terms constraints that shape the design

Paraphrased; read the originals.

1. **No ML/AI with Spotify content** (Terms IV.2.1, Policy III.14): no training, and no ingesting
   Spotify content into a model. → Classification is rule-based and transparent; no Spotify
   data is sent to LLMs, including through AI coding assistants analyzing exported data.
2. **"Do not analyze the Spotify Content or the Spotify Service for any purpose"** (Policy III.13).
   This is broad. Phase 1 only stores and counts your own library to show it back to you.
   Phases 3–4 score songs against playlist identities you write, using your approved
   examples, your feedback, and independently sourced metadata. Whether that counts as
   "analyzing Spotify Content" is a judgment call. **Decided 2026-10-17: the owner chose
   option 2 in `data_sources.md`** (per-song ISRC / artist + title lookups to MusicBrainz and
   Last.fm; scoring works on their tags).
3. **Store only what is needed and not indefinitely; delete on disconnect** (Terms IV.3, V.7–8).
   → Tracks no longer in any source are deleted on each import; `curator purge` removes everything.
4. **Don't transfer Spotify data to other services** except a user's own data/playlist metadata
   (Policy III.9). → External metadata lookups (Phase 3) should send the minimum identifier
   needed (an ISRC, or artist + title), never bulk exports, and should be reviewed against this clause.
5. **Personal, non-commercial use.** This project is exactly that; it is not to be offered as a service.
