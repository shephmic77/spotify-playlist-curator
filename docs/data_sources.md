# Data sources

## Decision (2026-10-17)

Spotify Policy III.13 prohibits analyzing Spotify Content "for any purpose", and III.9 limits
transferring data to other services. Three options were considered:

1. User input only (no external lookups).
2. **Plus identifier lookups** — send individual ISRCs (MusicBrainz) or artist + title (Last.fm)
   to fetch *their* independently produced descriptors; scoring operates on those.
3. Plus local audio analysis of files the owner has rights to.

**The project owner chose option 2.** Option 3 is not planned.

## In use

| Source | Data | What we send | Limits honoured | Stored |
|---|---|---|---|---|
| Spotify Web API | Liked Songs, readable playlists, track name/artists/album, release date + precision, duration, ISRC, local-file flag | — | see `spotify_api.md` | tables `tracks`, `source_entries`, `playlists` |
| MusicBrainz (`/ws/2/isrc/{isrc}`, `/ws/2/artist/{mbid}`) | Recording + artist community tags and genres, recording first-release date, MusicBrainz ids | one ISRC per request; artist MBID | ≤ 1 request / 1.1 s; `User-Agent: PlaylistCurator/<ver> ( <contact> )`; 503 → back off | trimmed: tag names + counts, first-release date, ids. Refreshed after 90 days |
| Last.fm (`track.getTopTags`, `artist.getTopTags`) | Track and artist top tags | artist name + cleaned title; artist name | API key required (`LASTFM_API_KEY`); non-commercial use; ≤ 4 requests/s; error 29 → back off; HTTP `Cache-Control` honoured | top ≤ 40 tag names + counts per lookup (well under the 100 MB cap). Refreshed per `Cache-Control`, else 30 days |

| AcousticBrainz (`/api/v1/high-level` and `/api/v1/low-level`, bulk) | Audio-derived classifier probabilities per recording (danceable, relaxed, happy, sad, aggressive, party, acoustic, electronic, instrumental; timbre bright/dark; vocal gender; MIREX mood clusters) and measurements (`rhythm.bpm`, `rhythm.onset_rate`, `tonal.key_scale` + strength, `lowlevel.dynamic_complexity`). Thresholds: binary classes ≥ 0.6; timbre ≥ 0.7; gender ≥ 0.75 and only with vocals; mood cluster ≥ 0.45; tempo < 85 slow, ≤ 115 mid-tempo, ≥ 135 fast; key only with strength ≥ 0.6; onset rate ≥ 5.5 busy, ≤ 2 minimal; dynamic complexity ≥ 6 dynamic | MusicBrainz recording ids only (≤ 25 per request) | `X-RateLimit-*` headers honoured (typically 10 requests / 10 s); same User-Agent as MusicBrainz | the 9 probabilities per recording. Refreshed after a year (the dataset stopped growing in 2022; data CC0) |

Attribution: the CLI (`enrich`, `analyze`) and the UI's Playlist analysis tab credit
MusicBrainz and Last.fm with links.

Licensing notes: MusicBrainz core data is CC0; other parts of its database are released under a
Creative Commons licence (see musicbrainz.org/doc/About/Data_License; this project hasn't
confirmed which part tags fall into). Personal, non-commercial use fits either. Last.fm data
is used under its API Terms (non-commercial, attribution, 100 MB cap).

## How descriptors are built

- Tags are normalized (`metadata/tags.py`): lowercase, aliases (hip-hop → hip hop, synth-pop →
  synthpop), decades unified ("1980s", "80's", "eighties" → 80s), and tagger-centric tags dropped
  ("seen live", "favorites", "under 2000 listeners", the artist's own name).
- Each source's counts are scaled to 0–1 against its strongest tag; tags under 0.1 are dropped.
- **For matching songs to playlists, each song is judged as an individual**
  (`metadata/vibes.py`, owner's decision 2026-10-21): only song-level sources count (Last.fm
  track tags, MusicBrainz recording tags, AcousticBrainz audio measures). Every tag is read as
  *vibe* (always counts), *genre* or *era* (count only in playlists whose definition mentions
  them), *ignore*, or *other* (half weight). Sound-describing genres add a softer vibe word
  (jazz → jazzy 0.6, metal → heavy 0.6). AcousticBrainz probabilities ≥ 0.6 become vibe words
  at 0.7 × (p − 0.5) / 0.5. Artist tags are used only for songs with no song-level data at all,
  at 0.5 weight and flagged. The owner's own tag classes (`tag_classes`) override the rules.
- `combined = max(song-level weight, 0.6 × artist-level weight)` is still used to describe old
  playlists (`curator analyze`), not for matching.
- Original release year = earliest of MusicBrainz first-release year and Spotify release year
  (compilations and remasters otherwise make old songs look new).
- No tag is ever inferred. A song with no tags is "untagged" and excluded from tag-based measures.

Descriptors are derived from the cache on demand, so merging/splitting recordings never
triggers new requests.

## Your own input (Phase 3b onward, highest priority)

- Playlist identity text you write (concept, qualifies, disqualifies, exceptions, neighbors).
- Reference songs you approve per playlist and explicit exclusions.
- Tags you assign to songs or artists ("80s sound", "night drive").
- Accept/reject/essential feedback.

These outrank external tags. They're the only way to capture subjective categories like
"sounds like the 80s but is from 2013".

## Optional: Claude for describing playlists

`curator describe` can use the Anthropic API (`ANTHROPIC_API_KEY`, default model
`claude-haiku-5-5`) to turn a description you type into a tag recipe. The request contains only
that text and fixed instructions. No Spotify data (songs, artists, playlists, tags derived from
them) is sent, consistent with Spotify's prohibition on ingesting Spotify Content into AI models.
Without a key, a built-in dictionary is used. Matching songs to the recipe is local and
rule-based.

## Not used

- Spotify audio features / analysis / recommendations (unavailable to new apps, and Spotify
  content may not go into ML models).
- AcousticBrainz (stopped collecting in 2022) and local audio analysis (option 3 not chosen).
