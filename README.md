# Spotify Playlist Curator

A personal Python application that rebuilds a messy Spotify playlist collection from the music
you already have. It treats your **Liked Songs plus every playlist you own** as one Master Music
Crate, proposes a better set of playlists with explicit identities, and, once approved,
creates and maintains those playlists in your account without letting them drift.

It never adds music from outside your crate, never touches Liked Songs, and never modifies
or deletes your original playlists.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Spotify connection, Liked Songs + playlist import, inventory | **Done** |
| 2 | Master Music Crate: recording-level dedup, provenance, local files | **Done** |
| 3a | Tags from MusicBrainz + Last.fm; analysis of your existing playlists | **Done** |
| 3b | Playlist identities and proposed playlist collection | Next |
| 4 | Multi-label scoring and draft playlists | Planned |
| 5 | Feedback and personalization | Planned |
| 6 | Creating playlists in Spotify (preview → approve → write, rollback) | Planned |
| 7 | Automatic maintenance and drift control | Planned |
| 8 | Refinement | Planned |

See [docs/project_plan.md](docs/project_plan.md) for the full roadmap.

## What works today

- Secure login with OAuth Authorization Code + PKCE (no client secret; read-only scopes).
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

- **Tags from MusicBrainz and Last.fm** (Phase 3a, `curator enrich`): genre, era and mood tags
  for each song, fetched one song at a time within each service's rate limits and cached.
- **Analysis of your existing playlists** (`curator analyze`): each playlist's sound and era,
  cohesion, songs that don't fit, drift between early and later additions, staleness, and
  overlapping or redundant playlists.

## Important limitations (from Spotify, not this app)

- **Only playlists you own or collaborate on can be read.** Since February 2026, Spotify
  returns playlist *contents* only for those. Playlists you merely follow are listed but
  contribute no songs. Copy their songs into a playlist you own if you want them in the crate.
- **Development Mode:** the app owner (you) needs Spotify Premium; at most 5 users per app.
- **No audio features, audio analysis, or recommendations** for new apps, and Spotify's
  policy forbids feeding Spotify content to ML/AI models. Classification will rely on your own
  descriptions, approved example songs, feedback, and independent metadata.
- **Local files can be read but not added** to playlists through the API. Later phases produce
  a manual to-do list for the desktop app instead.

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
git clone <your repo> spotify-playlist-curator
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
curator ui                  # Streamlit interface at http://localhost:8501
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
pytest            # 128 tests; Spotify, MusicBrainz and Last.fm are mocked
ruff check . && ruff format --check .
```

Project conventions for human and AI contributors are in [AGENTS.md](AGENTS.md).
Architecture: [docs/architecture.md](docs/architecture.md).

## Privacy

Everything is stored locally in `data/`. Besides Spotify's own API, the only outside requests
are per-song tag lookups: an ISRC to MusicBrainz, and artist + title to Last.fm (if you set a key).
No Spotify data is sent to any AI model by this application.

Tag data comes from [MusicBrainz](https://musicbrainz.org) and [Last.fm](https://www.last.fm)
and is used for personal, non-commercial purposes.
