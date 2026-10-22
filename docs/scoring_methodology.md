# Scoring methodology

Implemented: existing-playlist analysis (3a), proposals (3b), draft scoring (4) and feedback (5,
`curator/scoring.py`, summarised at the end of this file). The design sections below are the
original plan; where they differ, the "implemented" sections are authoritative.

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
| Outliers | songs below max(0.15, min(cohesion − 2·sd, 0.5·cohesion)), lowest first, ≤ 10 |
| Drift | cosine between profiles of the first and second half of songs by date added (≥ 20 dated, tagged songs) |
| Overlap | Jaccard and containment (shared ÷ smaller) of recording sets; profile cosine |

Flags (heuristic thresholds, not calibrated): fewer than 8 songs; nothing added in 365 days;
cohesion < 0.30 (broad/mixed); drift similarity < 0.60; containment ≥ 0.6, Jaccard ≥ 0.4, or
profile cosine ≥ 0.90 between playlists. These thresholds will be revisited against your
feedback in Phase 5.


## Proposal engine (implemented, `curator/discovery.py`)

- Concepts from descriptions: songs of the described old playlists (minus analysis misfits)
  are example songs; rule tags = distinguishing tags of those examples (support ≥ 0.25,
  ranked by support × log2(1 + lift), top 8, weight = support).
- Library clusters: tags on ≥ 3 songs are dimensions; rows L2-normalised; spherical k-means
  (k-means++ seeding on cosine distance, 4 restarts, seed 42); k in 6–18 chosen by mean
  silhouette (cosine); clusters < 15 songs dropped. A cluster with cosine ≥ 0.80 to a described
  concept counts as covered (shown as supporting evidence); others become proposals.
- Described concepts with profile cosine ≥ 0.85 are flagged as similar.


## Draft scoring (implemented, `curator/scoring.py`)

Per song *s*, per playlist *p* (only playlists with auto-maintain on; hand-picked playlists hold
exactly their example songs):

1. Essential → in. Excluded → out. Untagged → out (never guessed).
2. Required tags: needs one at weight ≥ 0.3, else out.
3. Tag score = Σ wₜ·idfₜ·sₜ / Σ wₜ·idfₜ, where idfₜ = ln((N+1)/(dfₜ+1)) + 1 (df = songs with
   the tag at ≥ 0.3), so specific tags ("nu metal") count more than common ones ("rock").
4. Example score = mean cosine similarity (on idf-weighted tag vectors) to the 3 most similar
   example songs, excluding *s*.
5. Score = mean of the available parts.
6. Avoided tag ≥ 0.5 → out; weaker → score × (1 − weight).
7. Preferred years: known original year outside the range → × 0.75; unknown → no change.
8. Threshold per playlist = 30th percentile of its example songs' scores (leave-one-out),
   clamped to 0.30–0.75 (0.45 if fewer than 3 usable examples), then raised if needed so
   that at most 20 % of the tagged library qualifies (a definition that broad isn't an
   identity; the draft notes it). In ≥ threshold; review within 0.08 below.

Song vectors here are the song's own sound (see `data_sources.md`): vibe words always, genre /
era labels only for the playlist that names them (its similarity matrix includes just those
label columns).

**Coverage** (default on, `draft --coverage`): after all playlists are scored, every song that
cleared no bar is placed in the auto-maintained playlist where score ÷ threshold is highest
(×1.1 for the playlist it was in last draft, for stability), with the reason "closest match
(weak fit)". Not where it was rejected; not into hand-picked playlists; songs with no data at
all stay unplaced.

Examples get no free pass, so misfits from old playlists can drop out. Overlapping drafts
(≥ 70 % of the smaller playlist shared; top 10) are reported. Scores rank and explain; they are not
probabilities.

## Describe (implemented, `curator/describe.py`)

Offline dictionary of ~20 situation/mood term groups → weighted tags + avoided tags;
"no X / without X / not (too) X" adds X to avoid; "lyrics are fine / can still have lyrics"
caps "instrumental" at 0.4; decades in the text → preferred years. Optional Claude engine
returns the same JSON shape, validated and clamped. Examples you name are resolved locally
(artist exact match incl. split credits, then song title exact / prefix / fuzzy ≥ 0.85); tags
shared by ≥ 50 % of examples are added at 0.8 × support. Grounding reports how many library
songs carry each tag and suggests close library tags for misses.


## Feedback (implemented, `curator/feedback.py`)

Per playlist only. keep → example song (role `reference`); reject → `exclude`; essential →
`essential`; uncertain → recorded, nothing changes; undo → restores the role the song had
before any feedback. Manual decisions always beat scores.

Effects on scoring:
- Excluded songs are never included; essential always.
- Negative examples: if a song's closest rejected song (cosine on idf-weighted vectors) is
  ≥ 0.85 similar and closer than its closest example, its score × 0.80, with a reason.
- Stability (entry vs exit bar): songs in the previous draft stay while score ≥ threshold −
  0.08; new songs need ≥ threshold.
- Suggested avoid tags: tags on ≥ 50 % of ≥ 3 rejected songs and ≤ 15 % of examples. Shown,
  never applied automatically.

Metrics: acceptance rate = kept ÷ (kept + rejected) among songs the draft had included;
manual adds; added/removed per playlist between drafts; holdout agreement = for each keep /
reject, re-score with that decision hidden and check whether the rules alone agree. These are
counts and checks, not calibrated probabilities.
