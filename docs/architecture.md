# Architecture

## Principles

- **Separation of concerns.** Spotify access, data acquisition, classification, storage and UI are
  independent layers. Scoring can be tested without Spotify; a metadata source can be swapped
  without touching scoring.
- **Local-first.** One SQLite file in `data/`. No server, containers, or cloud services.
- **Read before write, preview before apply.** Every Spotify write will be computed as a diff,
  shown, approved, and logged with the previous state.
- **Missing data is explicit.** `None` means unknown; nothing is filled with guesses.

## Layout (current)

```
src/playlist_curator/
  config.py            Settings from env/.env; scope sets; logging
  models.py            Pydantic models (Track, PlaylistSummary, SourceEntry, ...) + JSON parsers
  spotify/
    auth.py            PKCE login, local callback server, token store, refresh
    client.py          HTTP client: retries, 429, pagination, endpoint methods
  crate/
    importer.py        Fetch Liked Songs + playlists, then write in one transaction
    inventory.py       Read-only summaries and tables for CLI/UI
  storage/
    database.py        SQLite schema, migrations, queries, purge
  cli.py               `curator` command
  app.py               Streamlit UI (Library view)
tests/
  fake_spotify.py      httpx.MockTransport fake of the API
  fixtures/library.json
```

Planned modules (not yet present, created when their phase starts so nothing is a stub):
`crate/deduplication.py` (2), `metadata/providers.py`, `metadata/matching.py` (3),
`curator/playlist_discovery.py`, `classification.py`, `scoring.py` (3–4), `feedback.py` (5),
`spotify/sync.py` (6), `curator/maintenance.py`, `scripts/run_sync.py` (7).

## Data flow

```
Spotify API ──► spotify/client (raw JSON) ──► models.parse_* (normalized)
            ──► crate/importer (fetch all, then one transaction) ──► SQLite
SQLite ──► crate/inventory ──► CLI / Streamlit
```

Later:

```
SQLite (crate) + metadata providers + user identities/feedback
   ──► curator/scoring (per-playlist rules) ──► proposed membership + reasons
   ──► review UI ──► approved change set ──► spotify/sync (diff, apply, verify, log)
```

## Database (schema v1)

| Table | Purpose |
|---|---|
| `meta` | schema version, owning Spotify user id (prevents mixing accounts) |
| `tracks` | one row per URI (catalog or local). Includes ISRC, release date + precision |
| `playlists` | every playlist in your library; `contents_status` = fetched / unavailable / error |
| `source_entries` | each occurrence of a track in Liked Songs or a playlist (position, added_at, added_by) |
| `import_runs` | log of imports with counts and errors |

Phase 1 keys tracks by URI and deliberately does not merge recordings. Phase 2 adds a
`recordings` grouping layer on top, so merge decisions are reversible and auditable.

## Import algorithm

1. `GET /me`; refuse if the database belongs to another Spotify user.
2. Page through Liked Songs.
3. Page through `/me/playlists`. For each playlist: skip if `snapshot_id` matches the last
   successful fetch; else page through its items. 403/404 → `unavailable`; other errors →
   `error` (old data kept, retried next run); rate-limit → abort the whole import.
4. One transaction: upsert tracks, replace source entries per source, upsert playlists,
   forget playlists no longer in the library, delete tracks no longer in any source.
5. Record new/removed URIs vs. the previous run (the basis for Phase 7 change detection).

## Error handling

| Situation | Behavior |
|---|---|
| 401 | refresh token once, retry |
| 429, Retry-After ≤ 120 s | wait, retry (≤ 5 times) |
| 429, longer | `RateLimitedError`, import aborted, nothing written |
| 5xx / network | exponential backoff + jitter, ≤ 5 retries |
| 403/404 on playlist items | recorded as unavailable, import continues |
| Other 4xx on a playlist | recorded as error, previous data kept, run marked `partial` |
| Revoked refresh token | clear message: run `curator auth login` |
