# Data sources

Status: **Phase 1 uses only the Spotify Web API.** Everything below "Candidates" is a plan to
be verified (availability, licence, rate limits, terms) at the start of Phase 3. Nothing here
has been integrated yet.

## In use

| Source | Data | Notes |
|---|---|---|
| Spotify Web API | Your Liked Songs, your readable playlists, track name/artists/album, release date + precision, duration, explicit, ISRC, local-file flag | Stored locally only as long as needed; see `spotify_api.md` |

## Your own input (always allowed, highest priority)

- Playlist identity text you write (concept, qualifies, disqualifies, exceptions, neighbors).
- Reference songs you approve per playlist and explicit exclusions.
- Tags you assign to songs or artists ("80s sound", "night drive", "late-night piano").
- Accept/reject/essential feedback.

These carry most of the weight in scoring. They are the only way to capture subjective
categories like "sounds like the 80s but is from 2013".

## Candidates for independent descriptors (to verify in Phase 3)

| Source | What it might give | Things to verify |
|---|---|---|
| MusicBrainz | Recording ↔ ISRC links, artist relationships, community tags/genres, first-release dates (useful for "era of the original recording" vs. a remaster's date) | Licence of tags vs. core data; 1 request/second guideline and required User-Agent; coverage for your library |
| Last.fm API | Track/artist top tags (often mood/genre-like: "chillout", "synthwave") | API terms (non-commercial use, attribution, caching rules); tag noise |
| Your local audio files | Tempo/loudness/spectral features from files you hold rights to (e.g. via librosa) | Only for files you supply; never Spotify audio |
| AcousticBrainz dumps | Precomputed audio descriptors keyed by MusicBrainz ID | Project stopped collecting data in 2022; static dumps only; coverage gaps for newer music |

## Decision required before Phase 3

Spotify Policy III.13 prohibits analyzing Spotify Content "for any purpose", and III.9 limits
transferring data to other services. Options, from most to least conservative:

1. **User-input only:** scoring uses only your identities, tags, reference songs, and feedback,
   plus release dates shown back to you. No external lookups.
2. **Plus identifier lookups:** send individual ISRCs (or artist + title) to MusicBrainz/Last.fm
   to fetch *their* independently produced descriptors; scoring operates on those.
3. **Plus local audio analysis** of files you own.

The plan assumes option 2 unless you decide otherwise. It's your account and your call.
