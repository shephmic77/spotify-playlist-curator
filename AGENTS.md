# AGENTS.md — working rules for Claude Code, Codex, and humans

Read this before changing code. It records decisions that are easy to undo by accident.

## Mission

Reorganize the owner's existing Spotify music into high-quality, identity-stable playlists and
keep them that way. Quality and user control beat automation and coverage.

## Hard rules (do not violate, even if asked by a task description)

1. **Never introduce songs outside the Master Music Crate** (Liked Songs ∪ readable playlists).
   No search endpoints, no recommendations, no "similar artists" pulls.
2. **Never send Spotify content to an LLM or ML model**, and never train, fine-tune, or embed
   with it (Spotify Developer Policy / Terms IV.2.1). That includes using an AI coding agent
   to "classify" exported library data. Classification must be rule-based and transparent.
3. **Never modify Liked Songs.** The app requests no library-write scope.
4. **Never delete, rename, overwrite, or unfollow playlists the app did not create.** Writes
   go only to playlists listed in the app's own registry (`managed_playlists`; enforced by
   `sync._guard`). The importer skips those playlists so app output never feeds back in.
5. **First rebuild is preview-only.** Spotify writes require explicit approval in the UI/CLI.
   New playlists are private by default. Automatic maintenance (Phase 7) writes only when the
   owner turned on auto-apply, never creates playlists, and goes through `sync.apply`.
6. **Never claim a local file was added via the API.** `SpotifyClient` refuses local URIs.
7. **Never fabricate** tracks, API responses, mood/genre/energy descriptors, or evaluation
   scores. Missing data stays missing (`None`) and is surfaced as "unknown".
8. **Confidence scores are not probabilities** unless a calibration step exists and is tested.
9. **No secrets in git.** `.env`, `data/`, tokens, and library exports are git-ignored.
10. **Only documented Web API endpoints.** No scraping, no undocumented/internal endpoints.

## Architecture boundaries

```
spotify/   API access only (auth, HTTP, retries). Returns raw JSON. No business logic.
models.py  Pydantic models + parsers that normalize raw JSON. Pure functions.
crate/     Import, dedup, inventory. Depends on spotify/ + storage/.
metadata/  (Phase 3) Independent descriptor providers behind a common interface.
curator/   (Phase 3+) Playlist identities, scoring, feedback, maintenance. Must NOT import spotify/.
storage/   SQLite. Append-only migrations in MIGRATIONS.
app.py     Streamlit UI. Reads/writes through crate/ and curator/, never raw SQL for logic.
cli.py     Thin argparse layer.
```

The classification engine must stay independent of Spotify integration so a data source can be
swapped without touching scoring, and so scoring can be tested with fixtures alone.

## Spotify API facts this code relies on (verified 2026-10-17)

- Playlist entries: use `item` (fallback `track`, deprecated). Playlist objects: `items` (fallback `tracks`).
- `GET /playlists/{id}/items` returns 403 unless the user owns or collaborates on the playlist.
  This is expected; record as `contents_status='unavailable'`, do not treat as failure.
- Page size max 50 for reads; max 100 URIs per add/remove/replace.
- Track `popularity`, `available_markets`, `linked_from` are removed/deprecated. `external_ids.isrc` exists.
- `/me` no longer returns `country`; reads pass `market=from_token`.
- Redirect URI must use `127.0.0.1`, not `localhost`.

Re-verify against docs/spotify_api.md links before adding endpoints.

## External descriptor sources (Phase 3, owner chose option 2 in docs/data_sources.md)

- MusicBrainz: ≤ 1 request / 1.1 s, `User-Agent: PlaylistCurator/<ver> ( <contact> )`.
- Last.fm: needs `LASTFM_API_KEY`; non-commercial; credit Last.fm wherever its data is shown.
- Send one identifier per request (ISRC, or artist + title). Never bulk-export the library.
- Cache trimmed responses only (tag names + counts). Missing tags stay missing.

## Conventions

- Python 3.12+, type hints on public functions, `ruff check` + `ruff format` clean.
- Tests use `tests/fake_spotify.py` (an `httpx.MockTransport` fake) — never real credentials.
  Add fixture cases to `tests/fixtures/library.json` rather than ad-hoc dicts where possible.
- Any change to API usage needs a test asserting the request shape.
- Schema changes: append a new string to `MIGRATIONS`; never edit an applied migration.
- Importer: fetch everything first, then write in one transaction.
- Dedup: only identical ISRC merges automatically. Everything else is a suggestion. User
  decisions (`dedup_decisions`) are never derived or dropped; `recordings`,
  `recording_members`, `dedup_suggestions` are derived and rebuilt after every import/decision.
- The new collection is built **from scratch**. Old playlists and their descriptions are
  evidence; never propose keeping/renaming an old playlist or write to one.
- Hand-picked identities (`auto_maintain = 0`) never get songs added automatically.
- Feedback is per playlist and always beats scores. `feedback` / `feedback_log` are user data.
  Learning = examples, exclusions, negative-example penalty and *suggested* avoid tags; never a
  trained model.
- `describe` may send **only the user's typed description** to Claude. Never include song,
  artist, playlist, tag or any other library data in that request. Examples are resolved
  locally. Song-to-playlist matching stays rule-based (`curator/scoring.py`).
- Later phases reference **recordings** (`recording_id`), not URIs. Write the recording's
  `primary_uri` to Spotify.
- Log with `logging.getLogger(__name__)`; never log tokens or full Authorization headers.
- Update `docs/` and the Status table in README when a phase milestone lands.

## Commands

```bash
pip install -e ".[ui,dev]"
pytest
ruff check . && ruff format --check .
curator auth login | import | inventory | playlists | duplicates ... | local-files
curator enrich | analyze | intents ... | propose | proposals ... | identities ...
curator describe ... | draft | drafts [show|unassigned] | review | feedback | decisions
curator stats [--evaluate] | ui | purge
```
