"""CLI for Phase 7: automatic maintenance, its settings, and the OS schedule."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from playlist_curator.config import WRITE_SCOPES, Settings
from playlist_curator.crate import deduplication as dedup
from playlist_curator.curator import maintenance as mt
from playlist_curator.metadata.providers import (
    AcousticBrainzProvider,
    LastfmProvider,
    MusicBrainzProvider,
)
from playlist_curator.spotify.auth import AuthError, TokenManager, TokenStore
from playlist_curator.spotify.client import SpotifyAPIError, SpotifyClient
from playlist_curator.storage.database import Database

TASK_NAME = "Playlist Curator maintenance"
log = logging.getLogger(__name__)


def _db(settings: Settings) -> Database | None:
    if not settings.db_path.exists():
        print("No data yet. Run: curator import")
        return None
    db = Database(settings.db_path)
    dedup.ensure_current(db)
    return db


def _providers(db: Database, settings: Settings) -> list[Any]:
    out: list[Any] = []
    if settings.lastfm_api_key:
        out.append(LastfmProvider(db, LastfmProvider.make_http(), settings.lastfm_api_key))
    out.append(MusicBrainzProvider(db, MusicBrainzProvider.make_http(settings.musicbrainz_contact)))
    out.append(
        AcousticBrainzProvider(db, AcousticBrainzProvider.make_http(settings.musicbrainz_contact))
    )
    return out


def _has_write_scopes(settings: Settings) -> bool:
    tokens = TokenStore(settings.token_path).load()
    return bool(tokens) and (not tokens.scope or set(WRITE_SCOPES) <= tokens.scopes)


def _onoff(v: bool) -> str:
    return "on" if v else "off"


def cmd_maintain(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    trigger = "scheduled" if args.scheduled else "manual"
    if args.scheduled and hasattr(sys.stdout, "reconfigure"):
        # the log file Windows redirects to may not be UTF-8; never crash over a song title
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    with db:
        cfg = mt.load_settings(db)
        if args.no_enrich:
            cfg.enrich = False
        apply: bool | None = True if args.apply else (False if args.preview else None)
        will_apply = cfg.auto_apply if apply is None else apply
        if will_apply and not _has_write_scopes(settings):
            print(
                "Your saved login can't edit playlists. Run `curator auth login` once more; "
                "doing a preview only this time.",
                file=sys.stderr,
            )
            apply = False
        say = print if not args.quiet else (lambda _m: None)
        try:
            settings.validate()
            client = SpotifyClient(TokenManager(settings))
            res = mt.run(
                db,
                client,
                providers=_providers(db, settings) if cfg.enrich else [],
                settings=cfg,
                trigger=trigger,
                apply=apply,
                progress=say,
            )
        except (AuthError, SpotifyAPIError, mt.sync.SyncError) as exc:
            msg = str(exc)
            if isinstance(exc, AuthError):
                msg += "  (log in again with: curator auth login)"
            path = settings.data_dir / "reports" / "latest-maintenance.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"Maintenance failed: {msg}\n", encoding="utf-8")
            print(f"Maintenance failed: {msg}", file=sys.stderr)
            return 1
    path = mt.save_report(settings.data_dir, res)
    print("\n" + mt.report(res))
    print(f"\n(saved to {path})")
    return {"ok": 0, "held": 0, "partial": 2}.get(res.status, 1)


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        cfg = mt.load_settings(db)
        runs = mt.last_runs(db, args.n)
    print(
        f"Automatic updates: {_onoff(cfg.auto_apply)}   "
        f"limit per playlist: {cfg.max_changes} songs or {cfg.max_change_share:.0%} "
        f"(whichever is larger)   hold all if library shrinks > {cfg.max_library_shrink:.0%}   "
        f"tag new songs: {_onoff(cfg.enrich)}"
    )
    print(f"Schedule: {_schedule_state()}\n")
    if not runs:
        print("No maintenance runs yet. Try: curator maintain")
        return 0
    for r in runs:
        s = r["summary"]
        held = len(s.get("held", []))
        applied = len(s.get("applied", []))
        ready = len(s.get("ready", []))
        print(
            f"  #{r['run_id']:<4} {r['started_at'][:16]}  {r['trigger']:<9} {r['mode']:<7} "
            f"{r['status']:<7} +{s.get('new_songs', 0)} new songs · "
            + (f"{applied} updated" if applied else f"{ready} ready")
            + (f" · {held} waiting for you" if held else "")
            + (f" · {len(r['errors'])} problem(s)" if r["errors"] else "")
        )
    latest = runs[0]
    if latest["summary"].get("held"):
        print("\nWaiting for your approval (preview with `curator sync`):")
        for c in latest["summary"]["held"]:
            print(f"  {c['playlist']}: +{c['add']} / -{c['remove']}  ({c['reason']})")
    for e in latest["errors"]:
        print(f"  ! {e}")
    return 0


def cmd_settings(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    on = {"on": True, "off": False, None: None}
    with db:
        try:
            cfg = mt.save_settings(
                db,
                auto_apply=on[args.auto_apply],
                max_changes=args.max_changes,
                max_change_share=args.max_share,
                max_library_shrink=args.max_shrink,
                enrich=on[args.enrich],
            )
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
    print(
        f"Automatic updates: {_onoff(cfg.auto_apply)}\n"
        f"Per playlist, changes up to {cfg.max_changes} songs or {cfg.max_change_share:.0%} "
        "(whichever is larger) go through on their own; bigger ones wait for you.\n"
        f"If your library shrinks by more than {cfg.max_library_shrink:.0%} in one run, "
        "everything waits.\n"
        f"Tag new songs during maintenance: {_onoff(cfg.enrich)}"
    )
    if cfg.auto_apply:
        print("New playlists are never created automatically; use `curator sync --apply`.")
    return 0


# ---------------------------------------------------------------------------
# Schedule (Windows Task Scheduler / cron). No background service of our own.
# ---------------------------------------------------------------------------


def _wrapper_path(project: Path) -> Path:
    return project / "data" / "run_maintenance.cmd"


def _write_wrapper(project: Path) -> Path:
    path = _wrapper_path(project)
    (project / "data" / "logs").mkdir(parents=True, exist_ok=True)
    path.write_text(
        "@echo off\r\n"
        f'cd /d "{project}"\r\n'
        f'"{sys.executable}" -m playlist_curator.cli maintain --scheduled --quiet '
        '>> "data\\logs\\maintenance.log" 2>&1\r\n',
        encoding="utf-8",
    )
    return path


def schtasks_args(wrapper: Path, every: str, at: str) -> list[str]:
    cmd = ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/TR", f'"{wrapper}"', "/ST", at]
    if every == "weekly":
        cmd += ["/SC", "WEEKLY", "/D", "SUN"]
    else:
        cmd += ["/SC", "DAILY"]
    return cmd


def _schedule_state() -> str:
    if sys.platform != "win32":
        return "see `curator maintain schedule` (cron)"
    try:
        out = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if out.returncode != 0:
        return "not scheduled (curator maintain schedule --install)"
    nxt = next((ln for ln in out.stdout.splitlines() if ln.startswith("Next Run Time")), "")
    return "scheduled" + (f"; {nxt.split(':', 1)[1].strip()}" if nxt else "")


def cmd_schedule(settings: Settings, args: argparse.Namespace) -> int:
    project = Path.cwd()
    if not (project / ".env").exists():
        print("Run this from the project folder (the one with your .env file).", file=sys.stderr)
        return 1
    if sys.platform != "win32":
        hour, minute = args.at.split(":")
        days = "0" if args.every == "weekly" else "*"
        print("Add this line with `crontab -e`:")
        print(
            f'{int(minute)} {int(hour)} * * {days} cd "{project}" && "{sys.executable}" -m '
            "playlist_curator.cli maintain --scheduled --quiet >> data/logs/maintenance.log 2>&1"
        )
        return 0
    if args.remove:
        out = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
        print("Schedule removed." if out.returncode == 0 else "No schedule to remove.")
        return 0
    cmd = schtasks_args(_wrapper_path(project), args.every, args.at)
    if not args.install:
        print(
            f"This would ask Windows Task Scheduler to run `curator maintain` {args.every} at "
            f"{args.at} (while you're logged in and the PC is on).\n"
            "It runs a preview unless automatic updates are on (curator maintain settings "
            "--auto-apply on).\n\nTo set it up: curator maintain schedule --install"
            + (" --every weekly" if args.every == "weekly" else "")
            + (f" --at {args.at}" if args.at != "09:00" else "")
        )
        return 0
    _write_wrapper(project)
    out = subprocess.run(cmd)
    if out.returncode != 0:
        print("Task Scheduler refused it; see the message above.", file=sys.stderr)
        return 1
    print(
        f"Scheduled: {args.every} at {args.at}. Log: data\\logs\\maintenance.log, "
        "summary: data\\reports\\latest-maintenance.txt, or `curator maintain status`.\n"
        "Remove it with: curator maintain schedule --remove"
    )
    return 0


def _time(value: str) -> str:
    try:
        h, m = value.split(":")
        if 0 <= int(h) < 24 and 0 <= int(m) < 60:
            return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use HH:MM, e.g. 09:00")


def register(sub: Any) -> None:
    p = sub.add_parser(
        "maintain", help="import, tag new songs, redraft, and update Spotify within limits"
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="write allowed changes this time")
    g.add_argument("--preview", action="store_true", help="never write, even if auto is on")
    p.add_argument("--no-enrich", action="store_true", help="skip tagging new songs")
    p.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--quiet", action="store_true", help="only print the summary")
    p.set_defaults(func=cmd_maintain)
    ms = p.add_subparsers(dest="maintain_command")

    q = ms.add_parser("status", help="settings, schedule and recent runs")
    q.add_argument("-n", type=int, default=10)
    q.set_defaults(func=cmd_status)

    q = ms.add_parser("settings", help="automatic updates and drift limits")
    q.add_argument("--auto-apply", choices=["on", "off"])
    q.add_argument("--max-changes", type=int, metavar="N", help="songs per playlist (default 10)")
    q.add_argument("--max-share", type=float, metavar="0-1", help="share per playlist (0.25)")
    q.add_argument("--max-shrink", type=float, metavar="0-1", help="library shrink limit (0.10)")
    q.add_argument("--enrich", choices=["on", "off"], help="tag new songs during maintenance")
    q.set_defaults(func=cmd_settings)

    q = ms.add_parser("schedule", help="run maintenance on a schedule (Task Scheduler / cron)")
    q.add_argument("--every", choices=["daily", "weekly"], default="daily")
    q.add_argument("--at", type=_time, default="09:00", metavar="HH:MM")
    q.add_argument("--install", action="store_true")
    q.add_argument("--remove", action="store_true")
    q.set_defaults(func=cmd_schedule)
