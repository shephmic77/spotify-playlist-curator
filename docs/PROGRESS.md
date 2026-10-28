# Progress log

What was tried, what was learned, and the rules in force now. Newest first. For how to run
the app see the README; for the full technical handoff see [HANDOFF.md](HANDOFF.md).

## Current rules (v1.5.2, 2026-10-28)

- **One playlist at a time, very selective.** The owner defines each playlist with a name,
  a description in his own words, and example songs or artists. Defaults: **strict** style
  (bar × 1.1, at most 15% of the library per playlist) and **no forced filler** (coverage off).
- **Each song is judged by its own sound**: the song's own Last.fm / MusicBrainz tags and its
  AcousticBrainz audio measures, read as vibe words. Artist tags are a flagged last resort.
- **Genre and era count only when the playlist names them** ("rock", "grunge", "2000s"); a
  playlist can *require* a genre family (e.g. must be tagged rock / alternative rock / grunge /
  post-grunge).
- **Artist examples mean "their songs that fit"**: `--like "Radiohead"` brings only the
  Radiohead songs that match the description, not their whole catalogue.
- **The owner's edits teach the app**: songs removed in Spotify become rejects (never re-added,
  similar songs scored down there); songs added become keeps.
- Safety rules unchanged (AGENTS.md): only playlists the app created are ever written; Liked
  Songs and original playlists never touched; nothing from the library is sent to an AI.

## Iteration 3: selective, one by one (2026-10-28, v1.5.1-1.5.2)

**Decision:** "Them being very broad and having to use every song at least once was dumb."
Scrap everything again; build playlists one at a time, very selectively.

**First playlist, "2000 Rock":** 90s-2010s rock centred on the 2000s (fewer 90s and 2010s),
grunge / alternative / post-grunge; rough and rocky but able to turn more peaceful, carefully
(Red Hot Chili Peppers, Smashing Pumpkins). Examples: Nirvana, Smashing Pumpkins, Red Hot
Chili Peppers, Semi-Charmed Life, Mr. Jones, Radiohead, Creed, Foo Fighters.

**Built:** artist examples filtered to fitting songs; "post-grunge", "dynamic" understood;
strict + no filler are the defaults; `start-over` resets drafting settings to the defaults.

**Learned:** a word like "quiet" in a description triggers the built-in calm preset (soft,
mellow; avoid heavy). Describe the core sound; let examples carry the softer side.

## Iteration 2: 16 broad playlists, every song placed (2026-10-21 to 10-27, v1.1-1.5.0)

**Decision:** scrap the first collection; judge songs by their own sound, not artist, genre or
era (example: MF DOOM's "Lavender Buds" is chill and jazzy, not rap); every song in at least
one playlist.

**Playlists:** 80s, Chill, Indie, Sleep, Study, Classical, Workout, Groove, Rap, Heavy, Riffs,
Game, Folk, EDM, Drive, Alt Rock.

**Built along the way:** start-over; tag classes (vibe / genre / era / ignore, ~430 vibe words,
~160 genres that imply a sound); AcousticBrainz audio data (tempo, key, mood, vocals, timbre);
junk-tag filtering (radio stations, playlist names); related words ("relaxed" ≈ "chill");
automatic example picking; inclusive / balanced / strict styles; duplicate-name protection,
`identities dedupe`, `sync prune`; `describe --redefine`.

**What went wrong, and why:**
- First draft was lopsided (Study 498 songs, Workout 0): description words ("energetic",
  "chill") didn't meet the data's words ("party", "relaxed"), and leftovers all went to the
  broadest playlist. Fixed with related words and rank-based placement.
- The describe block was run twice, creating every playlist twice; `--create` now refuses an
  existing name.
- Rap and Alt Rock descriptions produced no tags at all until typed genre/era words were read.
- Forcing every song into some playlist made playlists broad and mixed; the owner pruned
  Groove (removed slow, calm songs: Fireworks, Aquatic Ambiance, Yesterday, My Way, Hide,
  Strangers in the Night, Mr. Sandman) and concluded the approach was wrong.

**Owner's notes kept for later playlists:** Groove = upbeat, energetic, head-bobbing, "get lost
in the groove", jazzy / soul with hooks; not slow or calm. Calm/Sleep separate from Study.
Guitar/Drums/Bass playlist = songs that highlight those instruments (e.g. "Can't Stop").
90s-2000s rock should include grunge and Red Hot Chili Peppers.

## Iteration 1: rebuild from old playlists (2026-10-17 to 10-19, v1.0.0)

**Built (7 phases):** Spotify import, recording-level dedup, MusicBrainz / Last.fm tags,
analysis of old playlists, proposals from the owner's descriptions of them, scoring with
reasons, keep / reject feedback, Spotify sync with snapshots and rollback, scheduled
maintenance with drift guards.

**Result:** 21 playlists created from proposals based on the old single-letter playlists.

**Why it was scrapped:** playlists followed artist, genre and era instead of how each song
sounds, and many were too broad (Indie chill 256, Car with parents 240, Guitar 221).

## Data state

1,248 recordings (Liked Songs + 31 owned playlists), 5 local files. Last.fm tags for ~1,227,
MusicBrainz matches for 1,156, AcousticBrainz audio data for 1,029. Backups from each
start-over are in `data\backups\` on the owner's PC.
