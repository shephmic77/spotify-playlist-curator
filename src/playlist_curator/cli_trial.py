"""CLI: try songs that aren't in your library yet, in one app-made playlist."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from playlist_curator.config import Settings
from playlist_curator.crate import deduplication as dedup
from playlist_curator.curator import identities as idn
from playlist_curator.curator import trials
from playlist_curator.spotify.auth import TokenManager
from playlist_curator.spotify.client import SpotifyAPIError, SpotifyClient
from playlist_curator.storage.database import Database

STATUS_TEXT = {
    "added": "added (pending: in the playlist only, not in your library)",
    "already_trial": "already being tried",
    "in_library": "already in your library, so it's judged like your other songs",
    "disliked_before": "you disliked it before: not added again",
    "not_found": "not found on Spotify with that title and artist; nothing added",
}


def _client(settings: Settings) -> SpotifyClient:
    settings.validate()
    return SpotifyClient(TokenManager(settings))


def _db(settings: Settings) -> Database | None:
    if not settings.db_path.exists():
        print("No data yet. Run: curator import")
        return None
    db = Database(settings.db_path)
    dedup.ensure_current(db)
    return db


def _songs(args: argparse.Namespace) -> list[str]:
    songs = list(args.songs or [])
    if getattr(args, "file", None):
        lines = Path(args.file).read_text(encoding="utf-8").splitlines()
        songs += [ln.strip().lstrip("0123456789.)- ").strip() for ln in lines]
    return [s for s in songs if s and not s.startswith("#")]


def cmd_add(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    songs = _songs(args)
    if not songs:
        print('Name songs as "Title - Artist", or use --file list.txt', file=sys.stderr)
        return 1
    with db:
        try:
            ident = idn.get_identity(db, args.playlist)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        client = _client(settings)
        try:
            results = trials.add(db, ident, songs, client.search_tracks)
        except SpotifyAPIError as exc:
            print(f"Spotify search failed: {exc}", file=sys.stderr)
            return 1
    added = 0
    for r in results:
        added += r.status == "added"
        mark = {"added": "+", "not_found": "?", "in_library": "="}.get(r.status, "·")
        shown = r.label or r.query
        print(f"  {mark} {shown}: {STATUS_TEXT[r.status]}")
    print(f"\n{added} song(s) to try in '{ident.name}'. Nothing was sent to Spotify yet.")
    if added:
        print("Put them in the playlist: curator sync --apply")
        print(
            "Then listen. Like one: heart it in Spotify (or curator trial like NAME SONG). "
            "Don't like it: remove it from the playlist in Spotify (or curator trial dislike)."
        )
    return 0


def cmd_list(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        idents = {i.identity_id: i for i in idn.list_identities(db)}
        ident_id = None
        if args.playlist:
            try:
                ident_id = idn.get_identity(db, args.playlist).identity_id
            except LookupError as exc:
                print(exc, file=sys.stderr)
                return 1
        rows = trials.rows(db, ident_id)
    if not rows:
        print('No trial songs yet. Add some: curator trial add "Playlist" "Title - Artist"')
        return 0
    for status in ("pending", "liked", "disliked"):
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        print(f"{status} ({len(group)})")
        for r in group:
            who = idents.get(r["identity_id"])
            pl = who.name if who else "?"
            print(f"  {r['title']} — {', '.join(json.loads(r['artists_json']))}   [{pl}]")
    return 0


def _decide(settings: Settings, args: argparse.Namespace, decision: str) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            ident = idn.get_identity(db, args.playlist)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        targets: list[dict[str, Any]] = []
        if args.all_pending:
            targets = trials.rows(db, ident.identity_id, "pending")
        for q in args.songs or []:
            try:
                targets.append(trials.find(db, ident.identity_id, q))
            except LookupError as exc:
                print(exc, file=sys.stderr)
                return 1
        if not targets:
            print("Name the trial song(s), or use --all-pending.", file=sys.stderr)
            return 1
        for row in targets:
            if decision == "like":
                print(f"  liked: {trials.like(db, ident, row)}  (now in your library, pinned here)")
            else:
                print(f"  disliked: {trials.dislike(db, ident, row)}  (leaves the playlist)")
    print("\nApply it: curator draft  then  curator sync --apply")
    return 0


def cmd_like(settings: Settings, args: argparse.Namespace) -> int:
    return _decide(settings, args, "like")


def cmd_dislike(settings: Settings, args: argparse.Namespace) -> int:
    return _decide(settings, args, "dislike")


def register(sub: Any) -> None:
    p = sub.add_parser("trial", help="try songs that aren't in your library, in one playlist")
    tsub = p.add_subparsers(dest="trial_cmd", required=True)
    q = tsub.add_parser("add", help='look up songs ("Title - Artist") and add them to try')
    q.add_argument("playlist")
    q.add_argument("songs", nargs="*")
    q.add_argument("--file", help="a text file with one song per line")
    q.set_defaults(func=cmd_add)
    q = tsub.add_parser("list", help="trial songs: pending, liked, disliked")
    q.add_argument("playlist", nargs="?")
    q.set_defaults(func=cmd_list)
    for name, func, text in (
        ("like", cmd_like, "keep it: into your library, pinned in the playlist"),
        ("dislike", cmd_dislike, "drop it: out of the playlist, never added again"),
    ):
        q = tsub.add_parser(name, help=text)
        q.add_argument("playlist")
        q.add_argument("songs", nargs="*", help='"Title" or "Title - Artist"')
        q.add_argument("--all-pending", action="store_true")
        q.set_defaults(func=func)
