# Scoring methodology

The existing-playlist analysis (Phase 3a) is implemented; scoring of new playlists (Phase 4) is
still a design.

## Goals

- Multi-label: a song is scored independently against every playlist.
- Playlist-specific: each playlist defines which evidence counts and how much. A decade
  playlist and a mood playlist do not share a similarity function.
- Transparent: every score decomposes into named, sourced evidence the UI can show.
- Conservative: missing evidence lowers confidence; it never becomes an assumed value.

## Playlist identity (data model)

```
PlaylistIdentity
  name, concept (free text)
  rules: list[Rule]            # each: evidence type, condition, weight, required?/veto?
  reference_songs: set[recording_id]     # approved exemplars
  excluded_songs:  set[recording_id]     # never include (per playlist)
  essential_songs: set[recording_id]     # always include
  neighbors: list[playlist_id]           # for distinctiveness checks
  thresholds: entry, exit                 # entry > exit (hysteresis)
  version
```

Example rule kinds: `user_tag_in`, `external_tag_in(source)`, `release_year_between`,
`original_release_year_between` (MusicBrainz first release), `artist_in`,
`similar_to_references(by=tags)`, `version_marker_absent(['live'])`, `duration_between`.

The "80s" example becomes two different identities if the owner wants: *Released in the 80s*
(release-year rule) and *80s Sound* (user/external tags like "synthpop", "new wave",
"80s sound", plus reference songs, with release year deliberately unweighted).

## Score

For song *s* and playlist *p*:

```
score(s,p) = Σ w_r · e_r(s)  /  Σ w_r        over rules r of p whose evidence is available
coverage(s,p) = Σ w_r (available) / Σ w_r (all rules)
```

- `e_r(s) ∈ [0,1]` is the rule's evidence value (1 = satisfied).
- Veto rules (e.g. `version_marker_absent`) force score 0 when violated.
- Reference similarity: weighted Jaccard over the song's descriptor set vs. the union of
  reference songs' descriptors, using only permitted, independent descriptors.

## Decision

| Condition | Decision |
|---|---|
| manual essential / manual add | include (always) |
| manual reject / excluded | exclude (always) |
| score ≥ entry and coverage ≥ c_min | propose add |
| currently member and score ≥ exit | keep |
| currently member and score < exit | propose removal (review) |
| coverage < c_min or score near threshold | needs review |
| otherwise | not a member |

Entry > exit prevents borderline songs from flapping between syncs.

## Confidence

`confidence` = evidence coverage, a heuristic. It is **not** a calibrated probability and the UI
labels it that way. Calibration (e.g. isotonic against held-out decisions) may be added once
there are enough feedback decisions, and only then may it be called a probability.

## Drift, cohesion, redundancy

- Cohesion: mean pairwise descriptor similarity within a playlist; flag members far below the
  playlist median.
- Redundancy: overlap of memberships (Jaccard) **and** of rule definitions between playlists;
  flag pairs above a threshold for merge/split review. Overlapping membership alone is fine.
- Growth guard: report when a playlist's additions in a period exceed its historical rate, or
  when cohesion drops after additions.

## Feedback use

Feedback edits rules, reference sets, and weights for one playlist. A rejection in playlist A
never affects playlist B. No model is trained on Spotify data.


## Existing-playlist analysis (implemented, `curator/analysis.py`)

Vectors are per-recording combined tag weights (`data_sources.md`). Untagged songs are excluded
from tag measures and counted separately.

| Measure | Definition |
|---|---|
| Profile | mean tag vector of the playlist's tagged songs (needs ≥ 5 tagged) |
| Support of a tag | share of tagged songs with weight ≥ 0.3 for it |
| Lift | mean weight in playlist ÷ mean weight across the whole crate |
| Distinguishing tags | support ≥ 0.25, ranked by support × log2(1 + lift) |
| Cohesion | mean cosine similarity of songs to the profile (0–1) |
| Outliers | songs below max(0.15, cohesion − 2·sd), lowest first, ≤ 10 |
| Drift | cosine between profiles of the first and second half of songs by date added (≥ 20 dated, tagged songs) |
| Overlap | Jaccard and containment (shared ÷ smaller) of recording sets; profile cosine |

Flags (heuristic thresholds, not calibrated): fewer than 8 songs; nothing added in 365 days;
cohesion < 0.30 (broad/mixed); drift similarity < 0.60; containment ≥ 0.6, Jaccard ≥ 0.4, or
profile cosine ≥ 0.90 between playlists. These thresholds will be revisited against your
feedback in Phase 5.
