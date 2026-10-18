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

## Phase 3 — Playlist understanding (decision needed first)

Before starting, the owner decides how to treat Spotify Policy III.13 ("do not analyze Spotify
Content"). See `spotify_api.md` and `data_sources.md`. Proposed scope:

- Old-playlist report from data we're allowed to show back: size, staleness (last added_at),
  overlap matrix (Jaccard between playlists), release-year spread, artist concentration.
  This surfaces drift and redundancy without any descriptor guessing.
- Playlist **identity editor**: concept, qualifying traits, disqualifying traits, exceptions,
  neighbors, plus approved reference songs and explicit exclusions.
- Independent descriptors (if approved): MusicBrainz tags/genres, Last.fm tags, user tags.
  Each descriptor keeps its source and is optional.
- Concept proposals: seeded from old playlists and from clusters in *descriptor* space; each
  proposal carries the evidence behind it, and the owner approves/edits/rejects.

## Phase 4 — Classification and curation

Per-playlist transparent scoring (see `scoring_methodology.md`), multi-label, with entry/exit
hysteresis, confidence as evidence coverage (not probability), "needs review" queue, cohesion
and redundancy checks, unassigned songs allowed.

## Phase 5 — Feedback

Accept / reject / uncertain / essential / never-in-this-playlist / manual add-remove,
per-playlist only. Manual decisions override scores permanently. Feedback adjusts
playlist-specific weights and reference sets. Holdout evaluation of acceptance rate.

## Phase 6 — Spotify playlist creation

Write scopes added. Registry of app-managed playlists (Spotify ID, identity version). Preview
→ approve → create private playlists → add in ≤100 chunks → re-read and verify → record
snapshot. Before any change: store previous state; compute minimal diff; detect manual edits
made in Spotify since last sync; rollback to saved states. Local-file to-do list.

## Phase 7 — Automatic maintenance

Detect crate changes (already recorded per import), score only affected songs, apply hysteresis,
produce change set, apply automatically only if the owner enabled it. Optional scheduling via
OS scheduler (cron / Task Scheduler) calling `scripts/run_sync.py`; no background service.

## Phase 8 — Refinement

Accuracy, API efficiency, UX, docs of known limitations.

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
