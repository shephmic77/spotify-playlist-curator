# Handoff: Spotify Playlist Curator (v1.2.2)

Everything a new coding agent (Claude Code, Codex) or a human needs to pick this project up.
Read this, then [AGENTS.md](../AGENTS.md) (hard rules and conventions), then the docs it links.
The owner's personal machine details (paths, playlist IDs, current settings) are in
`data/MY_SETUP.md` on his PC; `data/` is git-ignored, so that file is never on GitHub.

---

## 1. What this is

A personal Python app that rebuilt Michael Shepherd's messy Spotify collection from music he
already has, and keeps it organized:

1. **Imports** Liked Songs plus every playlist he owns or collaborates on into one local
   database, the **Master Music Crate**.
2. **Deduplicates** copies of the same recording (same ISRC merges automatically; near-matches
   are suggestions he confirms). Live, remix, sped-up etc. are never merged.
3. **Tags** every song from MusicBrainz and Last.fm (genre, mood, era), one lookup at a time,
   cached.
4. **Analyzes** his old playlists (sound, cohesion, outliers, drift, overlap) as evidence only.
5. **Proposes** a new collection from scratch: concepts from his descriptions of what the old
   playlists were for, plus clusters in the library's tags (spherical k-means + silhouette).
6. **Scores** every song against every approved playlist definition (rule-based, explainable),
   producing draft playlists with a reason per song.
7. **Learns from feedback** per playlist: keep / reject / essential / uncertain / undo, plus
   edits he makes directly in Spotify.
8. **Creates and syncs** the approved playlists in Spotify (private), only ever touching
   playlists the app created, with snapshots, verification and rollback.
9. **Maintains** them automatically on a schedule, within drift limits.

Status: **all 7 phases done**, plus v1.1 (judge songs by their own sound, every song placed, start-over). The collection is being rebuilt from scratch (section 8). 235 tests
pass, `ruff` clean. Version 1.2.2 (last tag `v1.0.0`).

---

## 2. Where everything lives

| What | Where |
|---|---|
| GitHub repo (source of truth) | <https://github.com/shephmic77/spotify-playlist-curator> (branch `main`, tag `v1.0.0`) |
| Working copy on his PC | `C:\Users\mikey\Desktop\Data Science projects\spotify-playlist-curator` |
| Secrets (never in git) | `.env` in the project folder (copy of `.env.example`, filled in) |
| His data (never in git) | `data\` in the project folder (see below) |
| Python | System Python 3.13 (`C:\Users\mikey\AppData\Local\Programs\Python\Python313\python.exe`), package installed editable with `pip install -e ".[ui,dev]"`; no virtualenv |
| Scheduled task | Windows Task Scheduler: **"Playlist Curator maintenance"**, daily 09:00, runs `data\run_maintenance.cmd` |
| Spotify app | <https://developer.spotify.com/dashboard>, Development Mode, redirect URI `http://127.0.0.1:8888/callback` |
| Last.fm API account | <https://www.last.fm/api/accounts> (key in `.env`) |
| claude.ai Project "Spotify" | holds `claude/spotify_api_findings.md` and a copy of this handoff |

Older copies that once lived in `Documents` were removed; the Desktop folder above is the only
working copy. Nothing for this project lives elsewhere on the PC.

### Contents of `data\` (local only)

| File | What it is | Safe to delete? |
|---|---|---|
| `curator.sqlite3` | The whole app state: crate, tags cache, playlist definitions, feedback, drafts, Spotify registry, snapshots, run logs (schema v9) | **No.** Feedback, decisions and the registry of app-made playlists are irreplaceable. Back it up before experiments |
| `.tokens.json` | Spotify OAuth tokens (owner-only permissions) | Yes (then `curator auth login` again) |
| `intents.json` | His descriptions of what each old playlist was for (also imported into the DB) | Keep as a record |
| `run_maintenance.cmd` | Wrapper Task Scheduler runs (cd + python -m ... maintain --scheduled) | Re-created by `curator maintain schedule --install` |
| `logs\maintenance.log` | Output of scheduled runs (created on the first scheduled run) | Yes |
| `reports\latest-maintenance.txt` | Plain-text summary of the last maintenance run | Yes |
| `MY_SETUP.md` | His personal setup notes (paths, playlist IDs, settings) | Keep |

### `.env` keys

`SPOTIFY_CLIENT_ID`, `SPOTIFY_REDIRECT_URI`, `CURATOR_DATA_DIR=./data`, `CURATOR_LOG_LEVEL`,
`LASTFM_API_KEY`; optional `MUSICBRAINZ_CONTACT`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`
(default `claude-haiku-5-5`). No client secret: login is PKCE.

---

## 3. Repository layout

```
spotify-playlist-curator/
├── README.md                 user-facing overview, setup, every command
├── AGENTS.md                 HARD RULES + conventions for AI agents and humans (read first)
├── CLAUDE.md                 Claude Code entry point (imports AGENTS.md, points here)
├── pyproject.toml            hatchling; deps: httpx, numpy, pydantic, python-dotenv;
│                             extras ui = streamlit, pandas; dev = pytest, ruff; script `curator`
├── .env.example              template for .env
├── .gitignore                .env, data/, *.sqlite3, tokens, caches
├── docs/
│   ├── HANDOFF.md            this file
│   ├── project_plan.md       what each phase delivered (history)
│   ├── architecture.md       module map, data flow, every DB table
│   ├── spotify_api.md        scopes, endpoints used, Feb-2026 API changes, sources
│   ├── data_sources.md       why MusicBrainz + Last.fm, rate limits, tag weighting
│   └── scoring_methodology.md  how songs are scored into playlists
├── src/playlist_curator/
│   ├── __init__.py           __version__
│   ├── config.py             Settings from env; READ_SCOPES / WRITE_SCOPES
│   ├── models.py             pydantic models + parsers for Spotify JSON
│   ├── spotify/
│   │   ├── auth.py           PKCE login, local callback server, token store, refresh
│   │   ├── client.py         HTTP client: retries, 429/Retry-After, paging (50), writes (100/req)
│   │   └── sync.py           plan / apply / verify / snapshots / rollback; registry guard
│   ├── crate/
│   │   ├── importer.py       fetch everything, then write in one transaction; skips app-made playlists
│   │   ├── deduplication.py  recordings via union-find on ISRC; suggestions; user decisions
│   │   └── inventory.py      read-only summaries for CLI/UI
│   ├── metadata/
│   │   ├── tags.py           tag cleaning, normalization, weighting
│   │   ├── providers.py      MusicBrainz, Last.fm, AcousticBrainz clients, rate limits, cache
│   │   ├── descriptors.py    per-recording descriptors from cache; `enrich` runner
│   │   └── vibes.py          judge songs individually: tag classes, song profiles
│   ├── curator/
│   │   ├── analysis.py       report on old playlists
│   │   ├── intents.py        his descriptions of old playlists (evidence)
│   │   ├── discovery.py      proposals: from descriptions + tag clusters
│   │   ├── identities.py     approved playlist definitions (tags, years, examples)
│   │   ├── describe.py       plain words -> tag recipe (offline lexicon, or Claude: text only)
│   │   ├── scoring.py        per-playlist scoring, adaptive thresholds, drafts
│   │   ├── feedback.py       decisions, undo, avoid-tag suggestions, stats, holdout check
│   │   └── maintenance.py    import -> tag new -> redraft -> plan -> drift guards -> apply
│   ├── storage/database.py   SQLite schema (append-only MIGRATIONS, now v9), queries, purge
│   ├── cli.py                `curator` entry point; registers the cli_* modules
│   ├── cli_concepts.py       intents / propose / proposals / identities
│   ├── cli_drafts.py         describe / draft / drafts
│   ├── cli_feedback.py       review / feedback / decisions / stats
│   ├── cli_sync.py           sync / rollback / local-files
│   ├── cli_maintain.py       maintain / status / settings / schedule
│   └── app.py                Streamlit UI (`curator ui`)
└── tests/                    235 tests; Spotify, MusicBrainz, Last.fm all faked
    ├── conftest.py, fake_spotify.py (stateful httpx.MockTransport fake incl. writes),
    │   fake_metadata.py, library_builder.py, fixtures/library.json
    └── test_*.py             one file per module area
```

About 10,000 lines of source. `curator/` must not import `spotify/` except
`curator/maintenance.py`, which orchestrates a whole run.

---

## 4. How it works (pipeline)

```
curator import ──► tracks, playlists, source_entries ──► recordings (dedup, derived)
curator enrich ──► metadata_cache (MusicBrainz ISRC lookups, Last.fm artist+title)
curator intents/propose/proposals accept ──► identities (playlist definitions)
curator describe ... --apply ──► identity tags / required / avoided / examples
curator draft ──► draft_runs + draft_members (include / review, score, reasons)
curator review / feedback ──► feedback, identity_examples (keep=reference, reject=exclude)
curator sync [--apply] ──► Spotify playlists + managed_playlists + playlist_snapshots
curator maintain ──► all of the above, unattended, within limits
```

### Scoring (curator/scoring.py)

- Song vector = combined tag weights (song-level tags, artist-level × 0.6), IDF-weighted so
  generic tags ("rock", "indie") count less.
- Score = blend of a **tag score** (overlap with the playlist's tag weights) and an **example
  score** (cosine to the closest approved examples, top 3).
- **Required tags** must be present; **avoided tags** veto above 0.5; songs outside preferred
  years × 0.75.
- **Adaptive threshold** = 30th percentile of the playlist's own examples' scores, clamped
  0.30–0.75 (0.45 default with < 3 examples). A review band of 0.08 below it.
- **Library cap**: a playlist holds at most 20% of the tagged library.
- **Hysteresis**: songs already in the previous draft have a lower bar to stay.
- **Negative examples**: within 0.85 similarity of a rejected song (and closer to it than to
  any example) × 0.80.
- Hand-picked playlists (`auto_maintain = 0`: Regular Show, Surf's up, Weird cool songs) are
  exactly their examples; nothing is ever added automatically.
- Scores are heuristics, **not probabilities**.

### Spotify sync (spotify/sync.py)

- Writes **only** to playlists in `managed_playlists` (`_guard` raises `NotManaged` otherwise).
- New playlists created private (`public: false` is sent explicitly; Spotify defaults to true).
- Diff by recording: never re-adds a song present under another copy; minimal add/remove; order
  untouched.
- Manual Spotify edits since the app's last write win: his adds become `keep`, his removes
  `reject` and are never re-added.
- Snapshot before and after every change; re-read to verify; `curator rollback "Name"`
  restores any snapshot. Deleted-in-Spotify playlists are re-created on the next sync.
- Local files can't be added via the API: listed by `curator sync local-files`.

### Maintenance (curator/maintenance.py)

One run: import → tag new or still-untagged songs only (backfill is `curator enrich`'s job) →
redraft → sync plan → drift guards → apply allowed changes only if auto-apply is on.
A playlist is **held** for approval when: it doesn't exist in Spotify yet (never auto-created);
the change exceeds `max(max_changes, max_change_share × size)` (defaults 10 songs / 25%); the
import was partial; or the library shrank more than `max_library_shrink` (10%). Settings live in
`meta.maintenance_settings` (JSON). Every run is logged in `maintenance_runs` (statuses:
running, ok, held, partial, failed, stopped) and summarized to `data/reports/`.

---

## 5. Database (SQLite, schema v9)

User data (never rebuild or drop): `dedup_decisions`, `playlist_intents`, `identities`,
`identity_examples`, `feedback`, `feedback_log`, `managed_playlists`, `playlist_snapshots`.
Derived (rebuilt freely): `recordings`, `recording_members`, `dedup_suggestions`, `draft_runs`,
`draft_members` (last 5 runs kept). Imported: `tracks`, `playlists`, `source_entries`. Logs:
`import_runs`, `sync_runs`, `maintenance_runs`. Cache: `metadata_cache`. Plus `meta`
(schema version, owning Spotify user id, maintenance settings) and `proposals`.
Full column-level description: [architecture.md](architecture.md).

Schema changes: append a new SQL string to `MIGRATIONS` in `storage/database.py`; never edit an
applied one.

---

## 6. Commands (cheat sheet)

```powershell
cd "$HOME\Desktop\Data Science projects\spotify-playlist-curator"

# everyday
curator maintain status                 # settings, schedule, recent runs, what's waiting
curator maintain                        # run maintenance now (writes only if auto-apply is on)
curator maintain --preview              # never write this time
curator sync                            # preview Spotify changes;  --songs  --only "Name"
curator sync --apply                    # write them (type yes)
curator rollback "Name" [--to ID]       # undo;  curator sync history "Name"
curator sync local-files                # local files to add by hand

# shaping playlists
curator drafts show "Name" --why        # what's in a draft and why
curator review "Name"                   # k / r / e / u song by song
curator feedback "Name" reject "Song"   # or keep / essential / uncertain / undo
curator describe "Name" "words..." --like "Artist" --apply   # redefine in plain words
curator identities edit "Name" --add-tag X --require-tag Y --avoid-tag Z
curator draft                           # rebuild drafts after changes
curator stats --evaluate                # acceptance, churn, holdout agreement
curator proposals                       # pending ideas (#24 Jazz / Oldies is pending)

# library + tags
curator import [--force]   curator enrich [--source lastfm|musicbrainz|acousticbrainz]
curator tags   curator tags song "Title"   curator tags set "tag" vibe|genre|era|ignore|default
curator draft --coverage on|off   curator start-over   curator analyze
curator duplicates review   curator inventory   curator local-files

# settings / schedule / auth
curator maintain settings --auto-apply on|off --max-changes N --max-share 0.25 --max-shrink 0.1
curator maintain schedule --install [--every weekly] [--at HH:MM]   |  --remove
curator auth login [--read-only|--manual]   curator auth status   curator ui

# development
pytest                       # 235 tests, no network
ruff check . && ruff format --check .
```

---

## 7. Hard rules (summary; full list in AGENTS.md)

1. Never introduce songs outside the crate (no search, no recommendations).
2. Never send Spotify content to an LLM or ML model (Spotify Developer Terms). `describe` may
   send only the text he typed. Matching stays rule-based.
3. Never modify Liked Songs (no library-write scope is requested).
4. Never write to, rename, delete or unfollow playlists the app didn't create.
5. Spotify writes need explicit approval; new playlists private; maintenance never creates.
6. Never claim a local file was added via the API.
7. Never fabricate data; missing stays missing. Scores are not probabilities.
8. Only documented Web API endpoints. No secrets in git.
9. **Git authorship:** every commit authored and committed as
   `Michael Shepherd <mikeystitch9@gmail.com>`, with **no** `Co-Authored-By`, `Claude-Session`
   or other AI attribution trailers. He must be the sole contributor on GitHub.

---

## 8. Current state (2026-10-21)

**Starting over.** On 2026-10-21 the owner decided to scrap the 21-playlist collection and
define a new one himself, with two new rules now built in:

1. **Every song judged as an individual, by its own sound** (`metadata/vibes.py`): song-level
   tags + AcousticBrainz audio measures as vibe words; artist tags not used (flagged fallback
   only); genre / era count only in playlists that name them ("80s", "rap").
2. **Every song in at least one playlist** (coverage; leftovers go to their closest playlist
   as weak fits).

Steps on his PC: `git pull` → `curator start-over` (backup to `data\backups\`, removes the 21
app-made playlists from Spotify, wipes definitions, decisions, old-playlist descriptions and
proposals) → `curator enrich` (finishes the MusicBrainz backfill, ~1 h, then AcousticBrainz,
which needs MusicBrainz ids) → define the new playlists with `curator describe ... --like ...
--create NAME` → `curator draft` → `curator review` → `curator sync --apply`.

Playlists he has in mind (2026-10-21): 80s, Chill, Indie, Calm / Sleep, Study, Classical,
Workout, Soul / Funk / Jazz, Rap, Heavier, Guitar, Drums & Bass (songs that highlight those
instruments, e.g. "Can't Stop"), Gaming / Hype, Country / Folk, EDM, Car, 90s–2000s Rock /
Grunge; ideas offered: Late Night, Golden Hour, Feel-Good, Sad, Groove, Rainy Day, Dreamy,
Slow Jams / R&B, Instrumental, Morning.

**Progress as of 2026-10-22 (stopped for the day):** `start-over` done (21 app playlists
removed from Spotify, backup `data\backups\curator-before-start-over-20261022T034843.sqlite3`).
Enrich: Last.fm cached; MusicBrainz stopped at ~850/1248 (resume with
`curator enrich --source musicbrainz`); AcousticBrainz has 1,029/1,248 songs. Coverage
1,229/1,248 tagged, 1,106 with song-level data. **No playlists defined yet**: next step is the
16-line `describe` block (names: 80s, Chill, Indie, Sleep, Study, Classical, Workout, Groove,
Rap, Heavy, Riffs, Game, Folk, EDM, Drive, Alt Rock), then `curator draft`. "Lavender Buds"
has only junk Last.fm tags and no AcousticBrainz data, so it's judged by artist only: pin it
with `curator feedback "Chill" essential "Lavender Buds"` once Chill exists.

**2026-10-24:** enrich complete (MusicBrainz 1,156/1,248, AcousticBrainz 1,029/1,248). The
16 playlists were created; first draft (#8) was lopsided (Study 498 songs, 440 weak; Workout 0;
Game, Drive, Chill 1-3) because description words ("chill", "energetic") didn't meet the data's
words ("relaxed", "party") and leftovers went to the broadest playlist. Fixed in v1.2.2 (related
words; rank-based leftover placement). Next: `git pull`, `curator draft`, then add 2-3 examples
per playlist (`curator feedback "Name" keep "Song"`).

Library: 1,248 recordings, 31 old playlists, 5 local files. Maintenance stays scheduled daily
09:00 with auto-apply on; with no playlists defined it changes nothing, and it never creates
playlists.

---

## 9. Known issues and good next tasks

1. **Scheduled run missed on 2026-10-19 09:00** (no log, no run recorded): the PC was most
   likely asleep or off. `schtasks` doesn't enable "run as soon as possible after a missed
   start". Fix once in PowerShell:
   `Set-ScheduledTask -TaskName "Playlist Curator maintenance" -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)`
   and ideally make `curator maintain schedule --install` apply this itself (needs testing on
   Windows; `cli_maintain.py`).
2. **Instrument playlists** (Guitar, Drums & Bass) have little to match on: tags rarely name
   instruments and AcousticBrainz has no instrument classifier. They depend on example songs.
3. ~~Broad / redundant playlists, proposal #24~~: obsolete after the start-over.
4. Tag classes are rules (`vibes.py`, ~430 vibe words, ~160 sound genres; v1.2); check `curator tags` / `curator tags songs` after enrich and reclassify any
   that are read wrongly (`curator tags set TAG vibe|genre|era|ignore`).
5. **MusicBrainz backfill**: `curator enrich --source musicbrainz` (~1 h at 1 req/s, resumable).
   Follow with `curator maintain --preview`: tags shift scores and cause a jump.
6. Possible extra guard: hold everything when many playlists change at once but the library
   barely changed (would have caught run #2).
7. ~20 songs have no tags anywhere and can't be matched; consider a manual-assignment path.
8. 5 local files must be added by hand in the desktop app.
9. `app.py` (~800 lines) could be split into one module per tab.
10. The Last.fm key was once pasted into a chat; rotate it at last.fm/api/accounts if wanted.
11. `pyproject.toml` license says "Proprietary - personal use"; there's no LICENSE file. Add
    one (e.g. MIT) only if he decides to.

---

## 10. Spotify API facts this code relies on (verified Oct 2026)

- Development Mode: owner needs Premium; max 5 users.
- Since Feb 2026: playlist contents only for playlists you own or collaborate on (403
  otherwise, recorded as `unavailable`); `/playlists/{id}/items` with entry field `item`;
  `POST /me/playlists` to create; no audio features / recommendations for new apps.
- Paging max 50; writes max 100 URIs per request; `DELETE .../items` body key `items`.
- Scopes: `user-library-read playlist-read-private playlist-read-collaborative
  playlist-modify-private playlist-modify-public`.
- Redirect URI must be `127.0.0.1`, not `localhost`.

Details and links: [spotify_api.md](spotify_api.md).

---

## 11. Starting a session in Claude Code or Codex

```powershell
cd "$HOME\Desktop\Data Science projects\spotify-playlist-curator"
git pull
claude          # Claude Code reads CLAUDE.md -> AGENTS.md
# or: codex     # Codex reads AGENTS.md
```

Good first prompt: *"Read docs/HANDOFF.md and AGENTS.md, run pytest, then help me with
<task>. Commit as Michael Shepherd with no AI trailers."* Before risky changes, copy
`data\curator.sqlite3` somewhere safe. Never run tests against the real `data\` folder (the
suite uses temp dirs and fakes; keep it that way).
