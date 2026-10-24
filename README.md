# Spotify Playlist Curator

A personal Python application that rebuilds a messy Spotify playlist collection from the music
you already have. It treats your **Liked Songs plus every playlist you own** as one Master Music
Crate, proposes a better set of playlists with explicit identities, and, once approved,
creates and maintains those playlists in your account without letting them drift.

It never adds music from outside your crate, never touches Liked Songs, and never modifies
or deletes your original playlists.

## How it works

```
Spotify (Liked Songs + your playlists)
   │  import, dedup by recording
   ▼
Master Music Crate (SQLite)  ◄── tags from MusicBrainz + Last.fm
   │  your descriptions + example songs → playlist definitions
   ▼
Rule-based scoring → draft playlists, each song with a reason
   │  your keep / reject / essential feedback (and edits you make in Spotify)
   ▼
Preview → approve → private playlists in Spotify (snapshots, verify, rollback)
   │
   └─ scheduled maintenance keeps them current, within drift limits
```

No machine learning and no AI ever sees your library: matching is transparent tag and example
scoring you can inspect with `curator drafts show "Name" --why`.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Spotify connection, Liked Songs + playlist import, inventory | **Done** |
| 2 | Master Music Crate: recording-level dedup, provenance, local files | **Done** |
| 3a | Tags from MusicBrainz + Last.fm; analysis of your existing playlists | **Done** |
| 3b | Proposals for a new collection (from your descriptions + library clusters); editable playlist definitions | **Done** |
| 4 | Describe playlists in your own words; score songs into draft playlists with reasons | **Done** |
| 5 | Feedback: keep / reject / essential per song, learning per playlist, metrics | **Done** |
| 6 | Create approved playlists in Spotify (preview, approve, write, verify, rollback) | **Done** |
| 7 | Automatic maintenance and drift control | **Done** |

All phases are complete. How each was built: [docs/project_plan.md](docs/project_plan.md).

## Features

- Secure login with OAuth Authorization Code + PKCE (no client secret). Write scopes only
  cover playlists; nothing can modify Liked Songs. `curator auth login --read-only` if you
  just want to browse.
- Token caching with automatic refresh and owner-only file permissions.
- Import of Liked Songs and all playlists in your library, with pagination, rate-limit
  handling (`Retry-After`), retries with backoff, and clear errors.
- `snapshot_id` caching: unchanged playlists are not re-downloaded.
- Provenance: every song records which sources it came from (Liked Songs and/or which
  playlists, position, date added, who added it).
- Local files are identified, kept, and never given fake Spotify IDs.
- An inventory report (CLI and Streamlit) showing liked vs. playlist-only songs, overlap,
  local files, repeated entries, possible duplicate recordings, and unreadable playlists.
- `curator purge` deletes all local data and tokens (Spotify's terms require this on disconnect).
- **Duplicate recordings are merged** (Phase 2). Copies with the same ISRC count as one song;
  near-matches (e.g. a remaster with a new ISRC) and local files that look like catalog songs are
  *suggested* for you to confirm. Live, remix, edit, acoustic, sped-up and similar versions are
  never treated as duplicates. Your decisions are kept permanently and override the ISRC rule.
- **Tags and audio data** (`curator enrich`): up to 40 Last.fm tags per song and MusicBrainz
  tags, plus AcousticBrainz audio data: classifiers (danceable, relaxed, happy, sad, aggressive,
  party, acoustic, electronic, instrumental, bright/dark timbre, female/male vocals, five mood
  clusters such as passionate or bittersweet) and measurements (tempo, major/minor key, how busy
  and how dynamic). Fetched within each service's rate limits and cached.
- **A rich vocabulary of about 430 sound words** (feelings, energy, rhythm, texture,
  production, instruments, voice, moments: falsetto, harmonies, fuzzy, jangly, 808s, riffs,
  bouncy, bittersweet, golden hour, windows down...), plus 160 genres that imply a sound
  ("boom bap" → drums and samples, "yacht rock" → smooth and breezy). `curator tags songs`
  lists every song with its descriptors.
- **Every song judged as an individual, by how it sounds** (`metadata/vibes.py`): only tags on
  the song itself and its audio measures count, as vibe words (chill, funky, mellow, upbeat,
  dreamy, instrumental...). Artist-level tags aren't used (except as a flagged last resort for
  songs with no song data). Genre and era words (rap, rock, 80s...) count **only in playlists
  that ask for them**, so MF DOOM's "Lavender Buds" can land in Chill instead of Rap. See how a
  song is read with `curator tags song "Lavender Buds"`; reclassify any tag with
  `curator tags set "indie" vibe`.
- **No hand-picking needed**: a playlist you only described gets example songs picked
  automatically from its description (the best-matching songs, one per artist). Your own
  examples and keep/reject decisions always come first.
- **Every song has a home**: a song that clears no playlist's bar goes into the playlist it's
  closest to, marked as a weak fit (`curator draft` shows how many). Never into a playlist
  where you rejected it; hand-picked playlists never get extras. Turn off with
  `curator draft --coverage off`.
- **Start over** (`curator start-over`): backs up everything, removes the playlists this app
  created from your Spotify library, and deletes your playlist definitions, decisions and
  old-playlist descriptions, so you can define a new collection from scratch.
- **A new collection, from scratch** (Phase 3b): describe what your old playlists were for
  (`curator intents`), then `curator propose` suggests concepts from those descriptions and
  from clusters in your library's tags, each with its evidence. Accepting one creates an
  editable playlist definition (concept, tags, preferred years, example songs).
- **Describe a playlist in your own words** (`curator describe`): "music to study to, chill,
  lyrics ok but not distracting, not loud" plus examples like `--like Nujabes --like "Luv(sic)"`
  becomes tag rules and example songs. A built-in dictionary covers common moods and
  situations; with `ANTHROPIC_API_KEY` set, Claude translates anything. Only your typed
  description is ever sent to Claude, never songs or anything from your library; examples
  are matched locally.
- **Draft playlists** (`curator draft`): every song is scored against every playlist's own
  definition (tags, examples, required/avoided tags, preferred years). Songs can land in
  several playlists or none; each gets a plain-language reason. Nothing goes to Spotify.
- **Your feedback shapes each playlist** (Phase 5): keep, reject, mark essential or unsure,
  per playlist (`curator review "Workout"`, or the Draft playlists tab). Kept songs become
  examples, rejected songs never return and similar songs are scored down in that playlist only,
  essential songs are always in. Songs already in a draft need less to stay than new songs need
  to get in, so playlists don't flip-flop. `curator stats --evaluate` shows acceptance rates,
  changes between drafts and a holdout check.
- **Your playlists in Spotify** (Phase 6): `curator sync` previews exactly what would be
  created or changed; `curator sync --apply` writes it after you type `yes`. New playlists are
  private. The app only ever writes to playlists it created itself (kept in a registry), saves
  each playlist's state before and after every change, sends minimal changes (never re-adds a
  song that's already there under another copy), re-reads to verify, and can roll back
  (`curator rollback "Name"`). Songs you add or remove in Spotify by hand are respected and
  count as keep / reject feedback. Local files go on a to-do list (`curator sync local-files`),
  because the API can't add them. Also available in the UI's *Spotify sync* tab.
- **Automatic maintenance** (Phase 7): `curator maintain` imports your library, tags only new
  and still-untagged songs (backfilling is `curator enrich`'s job), rebuilds the drafts and
  compares them with Spotify. By default it only prepares a preview; turn on `curator maintain settings --auto-apply on` to let small changes through on
  their own. Drift guards hold a playlist for your approval when it's new, when it would change
  by more than 10 songs or 25% (configurable), or when the import was incomplete or your
  library shrank by more than 10%. `curator maintain schedule --install` runs it daily (or
  `--every weekly`) with Windows Task Scheduler; no background service. `curator maintain
  status` shows recent runs and anything waiting for you.
- **Analysis of your existing playlists** (`curator analyze`): each playlist's sound and era,
  cohesion, songs that don't fit, drift between early and later additions, staleness, and
  overlapping or redundant playlists.

## Important limitations (from Spotify, not this app)

- **Only playlists you own or collaborate on can be read.** Since February 2026, Spotify
  returns playlist *contents* only for those. Playlists you merely follow are listed but
  contribute no songs. Copy their songs into a playlist you own if you want them in the crate.
- **Development Mode:** the app owner (you) needs Spotify Premium; at most 5 users per app.
- **No audio features, audio analysis, or recommendations** for new apps, and Spotify's
  policy forbids feeding Spotify content to ML/AI models. Classification relies on your own
  descriptions, approved example songs, feedback, and independent metadata (tags).
- **Local files can be read but not added** to playlists through the API. The app gives you a
  to-do list for the desktop app instead (`curator sync local-files`).

Details and sources: [docs/spotify_api.md](docs/spotify_api.md).

## Setup

### 1. Create a Spotify app

1. Go to <https://developer.spotify.com/dashboard> and log in with the Spotify account whose
   library you want to curate (it must have Premium).
2. **Create app.** Any name and description. For **Redirect URI** enter exactly:
   `http://127.0.0.1:8888/callback`
   (Spotify rejects `localhost`; the loopback IP is required.)
3. Under **Which API/SDKs are you planning to use?** select **Web API**. Save.
4. Open the app's **Settings** and copy the **Client ID**. You do **not** need the client secret.
5. If you will log in with a Spotify account other than the one that owns the app, add it under
   **User Management**.

### 2. Install

Requires Python 3.12+.

```bash
git clone https://github.com/shephmic77/spotify-playlist-curator.git
cd spotify-playlist-curator
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows (PowerShell):
#   .venv\Scripts\Activate.ps1
pip install -e ".[ui,dev]"
```

### 3. Configure

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Edit `.env` and set `SPOTIFY_CLIENT_ID`. Keep `SPOTIFY_REDIRECT_URI` identical to the one
registered in the dashboard. `.env` and `data/` are git-ignored.

### 4. Log in and import

```bash
curator auth login          # opens your browser; approve, then return to the terminal
curator import              # reads Liked Songs + playlists into data/curator.sqlite3
curator inventory           # summary of your crate
curator playlists           # which playlists were read, and which Spotify withheld
curator duplicates review   # confirm or reject suggested duplicate matches
curator enrich              # fetch tags (first run ~30-40 min; cached after that)
curator analyze             # report on your existing playlists
curator intents import data/intents.json   # or: curator intents set "W" "gym" --kind situation
curator propose             # proposals for the new collection
curator proposals accept 3 7 12   ·   curator proposals reject 5
curator identities          # your new playlists; `identities edit NAME --add-tag ...`
curator identities new "Classical" --like "Calm"   # create/copy a playlist without a proposal
curator identities edit "Classical" --require-tag classical   # only songs with that tag
curator describe "study music, chill, lyrics ok, not loud" --like Nujabes --create "Study"
curator draft               # fill every playlist with matching songs (drafts only)
curator drafts show "Study" --why    ·    curator drafts unassigned
curator review "Workout"    # k/r/e/u per song (borderline first; --all for everything)
curator feedback "Workout" reject "Island in the Sun"   # or keep / essential / uncertain / undo
curator stats --evaluate    # acceptance, rejections, changes, holdout agreement
curator ui                  # Streamlit interface at http://localhost:8501
curator auth login          # once more, to allow creating/editing the app's own playlists
curator sync                # preview what would be created in Spotify
curator sync --apply        # create them (private); asks you to type yes
curator maintain settings --auto-apply on    # optional: keep them current automatically
curator maintain schedule --install          # optional: run daily (Windows Task Scheduler)
```

If the browser can't reach the local callback (WSL, SSH, port in use), use
`curator auth login --manual` and paste the URL you were redirected to.

Re-run `curator import` any time. Only changed playlists are re-read; use `--force` to re-read all.

### Optional: Last.fm tags

MusicBrainz needs no key. For Last.fm (better mood/genre tags), create a free API key at
<https://www.last.fm/api/account/create> and add `LASTFM_API_KEY=...` to `.env`. Without it,
`curator enrich` uses MusicBrainz only. You can add the key later and run `enrich` again;
only missing lookups are fetched.

### Other commands

```bash
curator auth status         # who is logged in, granted scopes, token expiry
curator auth logout         # delete saved tokens (also remove access at spotify.com/account/apps)
curator duplicates list     # recordings with several copies (* = copy used for playlists)
curator duplicates same|different <id>   # decide one suggestion by id
curator duplicates split <uri>           # pull one copy out of its group
curator duplicates decisions | undo <id> # see / reverse your decisions
curator local-files         # local files, where they are, what they match
curator sync [--songs] [--only "Name"]   # preview changes to Spotify (writes nothing)
curator sync --apply        # write them (asks you to type yes)
curator sync managed        # playlists the app created, with links
curator sync history "Name" | curator rollback "Name" [--to ID]
curator sync release "Name" # stop syncing a playlist; it stays in Spotify as is
curator sync local-files    # local files to add to playlists by hand
curator maintain            # import + tag new + redraft + compare (writes only if auto is on)
curator maintain status     # settings, schedule, recent runs, what's waiting for you
curator maintain settings --auto-apply on [--max-changes 10 --max-share 0.25 --max-shrink 0.1]
curator maintain schedule --install [--every weekly] [--at 09:00]   # or --remove
curator tags [--class vibe]  # how tags are read: vibe / genre / era / ignore
curator tags song "Title"   # how one song is judged (its own sound, labels, audio data)
curator tags songs [--playlist "Name"]   # every song with its descriptors
curator tags set "tag" vibe|genre|era|ignore|default
curator draft --coverage on|off   # every song in at least one playlist (default on)
curator start-over          # backup, remove the app's playlists from Spotify, wipe definitions
curator purge               # delete local database + tokens; does not touch Spotify
curator -v import           # debug logging
```

## Your "Local Files" playlist

The API only sees local files that have been added to a **regular playlist you own**. Spotify's
built-in *Local Files* source in the desktop app is not a playlist and is invisible to the API.
After importing, `curator inventory` lists "Playlists containing local files". If your local
songs are missing, in the desktop app select all tracks in *Local Files*, right-click → *Add to
playlist* → a new playlist you own, then import again.

## Development

```bash
pytest            # 237 tests; Spotify, MusicBrainz, Last.fm, AcousticBrainz are mocked; no network
ruff check . && ruff format --check .
```

Project conventions for human and AI contributors are in [AGENTS.md](AGENTS.md); picking the
project up in Claude Code or Codex: [docs/HANDOFF.md](docs/HANDOFF.md).
Architecture: [docs/architecture.md](docs/architecture.md).

## Privacy

Everything is stored locally in `data/`. Besides Spotify's own API, the only outside requests
are per-song tag lookups (an ISRC to MusicBrainz, artist + title to Last.fm if you set a key)
and, only if you set `ANTHROPIC_API_KEY`, the description text you type into `describe`.
No Spotify data is sent to any AI model by this application.

Tag data comes from [MusicBrainz](https://musicbrainz.org) and [Last.fm](https://www.last.fm)
and is used for personal, non-commercial purposes.
