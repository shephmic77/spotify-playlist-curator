# Project plan

Primary objective: high-quality, personalized playlist organization with useful automation.
Coverage and automation are secondary.

## Phase 1 — Repository and Spotify connection ✅ (2026-10-17)

Delivered: PKCE auth, token refresh, resilient client (pagination, 429, backoff), import of
Liked Songs and readable playlists with provenance, snapshot caching, local-file detection,
inventory CLI and Streamlit Library view, purge, 60 tests with a mocked API.

Not yet verified against a live account (no credentials in the build environment). First
real run: see "Verification checklist" below.

## Phase 2 — Master Music Crate ✅ (2026-10-18)

Delivered:
- Recording layer over tracks: same ISRC merges automatically; nothing else does.
- Version-aware title parsing so live/remix/edit/acoustic/sped-up/re-recorded versions are
  never grouped with the original; edition labels (remaster, mono, "2011 Mix") are noted.
- Suggestions for near-matches with different ISRCs and for local files that look like catalog
  songs, with strength and a plain-language reason.
- Permanent user decisions (same / different / split / undo) that override the ISRC rule and
  survive re-imports.
- Primary-copy choice for future playlist writes.
- Inventory at recording level, missing-metadata counts, local-files report.
- CLI: `duplicates list|review|same|different|split|decisions|undo`, `local-files`.
- UI: Duplicates tab with Same/Different/Split/Undo buttons; Local files tab.
- 90 tests, including a v1 → v2 database upgrade.

Not verified here: the Streamlit tabs (Streamlit can't be installed in the build sandbox);
the logic behind them is covered by tests.

## Phase 3a — Descriptors and existing-playlist analysis ✅ (2026-10-18)

Owner chose option 2 (`data_sources.md`). Delivered:
- MusicBrainz (ISRC → recording tags, artist tags, first-release year) and Last.fm (track and
  artist top tags) providers with rate limiting, retries, and a trimmed local cache.
- Tag cleaning and per-recording descriptors (song-level vs. discounted artist-level).
- `curator enrich` (resumable) and `curator analyze`; Playlist analysis tab in the UI.
- Report per playlist: size, liked share, added dates, original-era spread, artist
  concentration, distinguishing tags (support × lift), cohesion, outliers, early-vs-late
  drift, plain-language flags; and overlapping/redundant playlist pairs.
- 128 tests.

Not verified here: live calls to MusicBrainz/Last.fm (not reachable from the build sandbox;
covered by mocked tests) and the Streamlit tab.

## Phase 3b — Proposals for a new collection ✅ (2026-10-18)

The owner wants the collection rebuilt from scratch; old playlists are evidence only.
Delivered:
- Descriptions of old playlists (`playlist_intents`) with a kind that decides their role:
  sound / mood / situation / era → concept seeds; handpicked → optional hand-picked playlist;
  source / catch_all / mixed / unknown → songs only.
- Proposal engine: concepts from descriptions (same-titled descriptions merge, e.g. two "indie
  chill" lists; misfits flagged by the analysis aren't used as examples; decades in the text
  become a preferred year range) plus concepts from tag clusters no description covers
  (spherical k-means, k by silhouette). Similar concepts are flagged, not merged.
- Accept → editable identity (name, concept, tags, avoid tags, preferred years, auto-maintain,
  example songs). Re-running `propose` never resurrects decided proposals.
- CLI (`intents`, `propose`, `proposals`, `identities`) and a "New collection" UI tab.
- 155 tests, including a smoke run of the whole Streamlit script against a stand-in module.

## Phase 4 — Describe in your words + draft playlists ✅ (2026-10-18)

- `curator describe`: description → tag recipe (offline dictionary, or Claude on the typed text
  only), examples resolved locally (`--like Nujabes --like "Luv(sic)"`), grounding against the
  library's tags, `--create` / `--apply`. UI form in the New collection tab.
- `curator draft`: per-playlist scoring (required/avoided tags, tag fit, similarity to examples,
  era preference, adaptive threshold, review band), reasons per song, overlap and unassigned
  reports, last 5 runs kept. `curator drafts show/unassigned`. Draft playlists UI tab.
- 176 tests.

## Phase 5 — Feedback and personalization ✅ (2026-10-18)

- Decisions per playlist: keep / reject / essential / uncertain / undo (`curator feedback`,
  interactive `curator review`, editable tables in the Draft playlists tab). Stored locally
  (`feedback`, `feedback_log`).
- Scoring respects decisions; rejected songs act as negative examples in that playlist;
  entry/exit bars for stability; changes between drafts reported.
- Avoid-tag suggestions from rejection patterns; `curator stats --evaluate` with acceptance
  rate, manual adds, churn and a holdout agreement check.
- 189 tests.

## Phase 6 — Spotify playlist creation ✅

Done: `spotify/sync.py`, `cli_sync.py`, *Spotify sync* UI tab, schema v7 (`managed_playlists`,
`playlist_snapshots`, `sync_runs`). Preview by default; apply needs write scopes and typed
confirmation. Writes guarded to the registry; before/after snapshots; diff by recording; manual
Spotify edits respected and recorded as feedback; verify by re-read; rollback to any snapshot;
deleted playlists re-created; release; importer skips managed playlists. 202 tests.

Original plan: Write scopes added. Registry of app-managed playlists (Spotify ID, identity version). Preview
→ approve → create private playlists → add in ≤100 chunks → re-read and verify → record
snapshot. Before any change: store previous state; compute minimal diff; detect manual edits
made in Spotify since last sync; rollback to saved states. Local-file to-do list.

## Phase 7 — Automatic maintenance ✅

Done: `curator/maintenance.py`, `cli_maintain.py`, maintenance section in the *Spotify sync*
tab, schema v8 (`maintenance_runs`), settings in `meta`. A run = import → enrich missing tags →
redraft (hysteresis) → sync plan → drift guards → apply allowed changes only if auto-apply is
on. Guards: never auto-create; per-playlist change limit max(N songs, share × size); hold all
when the import is partial or the library shrinks past a limit. Every run is logged and a
plain-text report saved to `data/reports/latest-maintenance.txt`. Scheduling via Windows Task
Scheduler (`schtasks`, wrapper in `data/run_maintenance.cmd`) or a printed cron line. Deviation
from the plan: the whole library is rescored each run (a second or two at this size) instead
of only affected songs; simpler and can't miss a song. 211 tests.

Original plan: Detect crate changes (already recorded per import), score only affected songs, apply hysteresis,
produce change set, apply automatically only if the owner enabled it. Optional scheduling via
OS scheduler (cron / Task Scheduler) calling `scripts/run_sync.py`; no background service.

## Evaluation metrics (from Phase 4)

Acceptance and rejection rates of suggestions, % crate assigned (secondary), unassigned count,
manual corrections per sync, membership churn between syncs, identity redundancy (overlap of
definitions and memberships), agreement with held-out approved examples.

## Verification checklist for the first live run

1. `curator auth login` completes and `curator auth status` shows the three read scopes.
2. `curator import` reports a liked count matching the Spotify app.
3. `curator playlists`: your own playlists show item counts; followed ones show `[x]`.
4. Your local-files playlist appears under "Playlists containing local files".
5. A second `curator import` reports all playlists unchanged and makes few requests.
