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
    deduplication.py   Group URIs into recordings; suggestions; user decisions
    inventory.py       Read-only summaries and tables for CLI/UI
  metadata/
    tags.py            Tag cleaning and weighting
    providers.py       MusicBrainz + Last.fm clients, rate limits, cache
    descriptors.py     Per-recording descriptors from the cache; `enrich` runner
  curator/
    analysis.py        Existing-playlist report: profile, cohesion, outliers, drift, overlap
    intents.py         Your descriptions of old playlists (evidence, never instructions to keep)
    discovery.py       Proposals: concepts from descriptions + tag clusters (spherical k-means)
    identities.py      Playlist identities: concept, tag rules, years, example songs
    describe.py        Your words -> tag recipe (offline dictionary or Claude, text only)
    scoring.py         Phase 4: per-playlist scoring, adaptive thresholds, draft playlists
  cli_concepts.py      CLI for intents / propose / proposals / identities
  cli_drafts.py        CLI for describe / draft / drafts
  storage/
    database.py        SQLite schema, migrations, queries, purge
  cli.py               `curator` command
  app.py               Streamlit UI (New collection, Old playlist analysis, Songs, Duplicates, Local files)
tests/
  fake_spotify.py      httpx.MockTransport fake of the API
  fixtures/library.json
```

Planned modules (not yet present, created when their phase starts so nothing is a stub):
`curator/feedback.py` (5),
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

## Database (schema v5)

| Table | Purpose |
|---|---|
| `meta` | schema version, owning Spotify user id (prevents mixing accounts) |
| `tracks` | one row per URI (catalog or local). Includes ISRC, release date + precision |
| `playlists` | every playlist in your library; `contents_status` = fetched / unavailable / error |
| `source_entries` | each occurrence of a track in Liked Songs or a playlist (position, added_at, added_by) |
| `import_runs` | log of imports with counts and errors |
| `recordings` | *derived* — one row per recording: id, primary URI used for writing, title |
| `recording_members` | *derived* — which URIs belong to which recording, and why (`self`, `isrc`, `accepted`) |
| `dedup_suggestions` | *derived* — pending possible matches (`title_match`, `local_match`) |
| `dedup_decisions` | **user data** — `same` / `different` per URI pair; never rebuilt, survives tracks leaving |
| `playlist_intents` | **user data** — what each old playlist was for (title, description, kind) |
| `proposals` | proposed concepts with evidence and a draft definition; pending / accepted / rejected |
| `identities` | **user data** — approved playlist definitions (name, kind, concept, rules, auto-maintain) |
| `identity_examples` | **user data** — example / excluded / essential songs per identity, keyed by URI |
| `draft_runs`, `draft_members` | *derived* — draft playlists from `curator draft` (include / review, scores, reasons); last 5 runs kept |
| `metadata_cache` | trimmed MusicBrainz / Last.fm responses keyed by provider + lookup key, with expiry. Descriptors are computed from it on demand |

Tracks stay keyed by URI. Recordings are a layer on top, rebuilt from tracks + decisions, so
every merge is reversible and auditable.

## Deduplication

1. Union-find over URIs. Edges: every pair sharing an ISRC (minus pairs you marked
   `different`), plus pairs you marked `same`.
2. Recording id: `isrc:<ISRC>` (lowest ISRC in the group) or `uri:<lowest URI>` if there's no
   ISRC or the ISRC id is taken by a split-off copy.
3. Primary URI (the copy written to playlists): catalog over local, then the copy in the most
   sources, then album > single > compilation, then playable, then earliest release.
4. Suggestions, never auto-merged:
   - `title_match`: two recordings whose representatives have the same normalized title,
     same primary artist, same *version signature*, and lengths within 10 s (≤ 3 s = strong).
   - `local_match`: a local file with the same normalized title and version signature as a
     catalog recording; artist must agree if the file has one; length within 20 s.
5. Title normalization strips accents, punctuation, "feat." credits and noise such as
   "(Official Video)". Qualifiers are read only from brackets and a trailing " - ..." part.
   Version words (live, remix, edit, acoustic, demo, instrumental, extended, sped up/slowed,
   re-recorded/"Taylor's Version", cover, orchestral) form the version signature; edition
   words (remaster, mono/stereo, deluxe, "from ... soundtrack", "2011 Mix", single/album
   version) don't, but are mentioned in the suggestion.

## Import algorithm

1. `GET /me`; refuse if the database belongs to another Spotify user.
2. Page through Liked Songs.
3. Page through `/me/playlists`. For each playlist: skip if `snapshot_id` matches the last
   successful fetch; else page through its items. 403/404 → `unavailable`; other errors →
   `error` (old data kept, retried next run); rate-limit → abort the whole import.
4. One transaction: upsert tracks, replace source entries per source, upsert playlists,
   forget playlists no longer in the library, delete tracks no longer in any source.
5. Record new/removed URIs vs. the previous run (the basis for Phase 7 change detection).
6. Rebuild recordings and suggestions.

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
