"""CLI for Phase 6: preview and apply changes to Spotify, roll back, local-file to-do list."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from playlist_curator.config import WRITE_SCOPES, Settings
from playlist_curator.crate import deduplication as dedup
from playlist_curator.curator import identities as idn
from playlist_curator.spotify import sync
from playlist_curator.spotify.auth import TokenManager, TokenStore
from playlist_curator.spotify.client import SpotifyClient
from playlist_curator.storage.database import Database


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


def _can_write(settings: Settings) -> bool:
    tokens = TokenStore(settings.token_path).load()
    if tokens is None:
        print("Not logged in. Run: curator auth login", file=sys.stderr)
        return False
    if tokens.scope and not set(WRITE_SCOPES) <= tokens.scopes:
        print(
            "Your saved login is read-only. Run `curator auth login` once more and approve "
            "the new permission (create and edit playlists the app makes).",
            file=sys.stderr,
        )
        return False
    return True


def _print_plan(p: sync.SyncPlan, *, songs: bool, label: Any = str) -> None:
    for pp in p.playlists:
        head = {
            "create": "NEW (private)",
            "update": "UPDATE",
            "unchanged": "no changes",
            "skip": "skip",
        }[pp.action]
        counts = ""
        if pp.action == "create":
            counts = f"{len(pp.add)} songs"
        elif pp.action in ("update", "unchanged"):
            counts = f"{pp.current_count} now → +{len(pp.add)} / -{len(pp.remove)}"
        print(f"  {pp.name[:28]:<28} {head:<14} {counts}")
        if pp.rename:
            print(f"      rename to '{pp.rename}'")
        if pp.keep_manual_adds:
            print(f"      you added {len(pp.keep_manual_adds)} song(s) in Spotify: kept")
        if pp.respect_manual_removes:
            print(
                f"      you removed {len(pp.respect_manual_removes)} song(s) in Spotify: "
                "won't be re-added"
            )
        if pp.local_files:
            print(
                f"      {len(pp.local_files)} local file(s) to add by hand (curator sync "
                "local-files)"
            )
        if pp.note:
            print(f"      {pp.note}")
        if songs and (pp.add or pp.remove):
            for u in pp.add[:100]:
                print(f"        + {label(u)}")
            for u in pp.remove[:100]:
                print(f"        - {label(u)}")


def cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            client = _client(settings)
            p = sync.plan(db, client, only=args.only)
        except sync.SyncError as exc:
            print(exc, file=sys.stderr)
            return 1
        first = not sync.managed(db)
        print(
            ("FIRST SYNC: " if first else "") + f"based on draft #{p.draft_run}. "
            "Only playlists this app creates are ever changed; your old playlists and "
            "Liked Songs are never touched.\n"
        )
        _print_plan(p, songs=args.songs, label=lambda u: sync._label(db, u))
        if not p.writes:
            print("\nNothing to change in Spotify.")
            return 0
        if not args.apply:
            print(f"\nPreview only: {p.writes} playlist(s) would change. Nothing was written.")
            print('To write it: curator sync --apply   (add --only "Name" to limit it)')
            return 0
        if not _can_write(settings):
            return 1
        if not args.yes:
            prompt = (
                f"\nWrite these changes to {p.writes} playlist(s) in your Spotify account? "
                "Type 'yes' to continue: "
            )
            if input(prompt).strip().lower() != "yes":
                print("Cancelled. Nothing was written.")
                return 1
        res = sync.apply(db, client, p, progress=print)
    print(f"\nSync {res.status}: {len(res.applied)} playlist(s) written and verified.")
    for e in res.errors:
        print(f"  ! {e['playlist']}: {e['error']}")
    print('Undo a playlist with: curator rollback "Name"')
    actions = [pp for pp in p.playlists if pp.local_files]
    if actions:
        print(
            f"{sum(len(pp.local_files) for pp in actions)} local file(s) need adding by hand: "
            "curator sync local-files"
        )
    return 0 if res.status == "ok" else 2


def cmd_local_files(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        items = sync.local_file_actions(db)
    if not items:
        print("No local files belong in your playlists.")
        return 0
    print(
        "Spotify's API can't add local files, so add these yourself in the desktop app\n"
        "(Your Library → Local Files → right-click the song → Add to playlist):\n"
    )
    for name, songs in items:
        print(f"  {name}:")
        for s in songs:
            print(f"    □ {s}")
    return 0


def cmd_managed(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        rows = db.conn.execute(
            "SELECT m.*, i.name AS identity_name FROM managed_playlists m "
            "JOIN identities i ON i.identity_id = m.identity_id ORDER BY m.name"
        ).fetchall()
        last = sync.last_sync(db)
    if not rows:
        print("The app hasn't created any Spotify playlists yet. Preview with: curator sync")
        return 0
    for r in rows:
        print(
            f"  {r['identity_name'][:28]:<28} {r['status']:<9} last synced {r['last_synced_at']}"
            f"   https://open.spotify.com/playlist/{r['spotify_id']}"
        )
    if last:
        print(f"\nLast successful sync: {last['finished_at']} ({last['status']})")
    return 0


def cmd_history(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            ident = idn.get_identity(db, args.playlist)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        snaps = sync.snapshots(db, ident.identity_id)
    if not snaps:
        print("No saved states yet.")
    for s in snaps:
        print(f"  #{s['snapshot_id']:<5} {s['taken_at']}  {s['reason']:<16} {s['songs']} songs")
    print('\nRestore one with: curator rollback "Name" --to SNAPSHOT_ID')
    return 0


def cmd_rollback(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            ident = idn.get_identity(db, args.playlist)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        if not _can_write(settings):
            return 1
        if (
            not args.yes
            and input(f"Restore '{ident.name}' in Spotify to an earlier state? Type 'yes': ")
            .strip()
            .lower()
            != "yes"
        ):
            print("Cancelled.")
            return 1
        try:
            out = sync.rollback(db, _client(settings), ident.identity_id, args.to)
        except sync.SyncError as exc:
            print(exc, file=sys.stderr)
            return 1
    print(
        f"Restored '{ident.name}' to saved state #{out['restored_snapshot']} "
        f"({out['songs']} songs). The state before this was saved too."
    )
    print(
        "Note: the next sync will apply your drafts again. Adjust them, or release the "
        'playlist (curator sync release "Name") to stop syncing it.'
    )
    return 0


def cmd_release(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            ident = idn.get_identity(db, args.playlist)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        sync.release(db, ident.identity_id)
    print(f"'{ident.name}' will no longer be changed by the app. It stays in Spotify as it is.")
    return 0


def cmd_start_over(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        reg = sync.managed(db)
        n_ids = db.conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0]
        print(
            "This will:\n"
            f"  1. back up everything to {settings.data_dir / 'backups'}\n"
            f"  2. remove the {len(reg)} playlist(s) this app created from your Spotify library\n"
            f"  3. delete your {n_ids} playlist definition(s) with their example songs and "
            "keep/reject decisions, the drafts, the old-playlist descriptions and proposals\n"
            "Kept: your library, your original playlists, Liked Songs, duplicate decisions, "
            "fetched tags and your tag choices.\n"
        )
        if reg and not _can_write(settings):
            return 1
        if (
            not args.yes
            and input("Type 'start over' to continue: ").strip().lower() != "start over"
        ):
            print("Cancelled. Nothing changed.")
            return 1
        client = _client(settings) if reg else None
        out = sync.start_over(db, client, backup_dir=settings.data_dir / "backups", progress=print)
    print(
        f"\nRemoved {len(out['removed'])} playlist(s) from Spotify; wiped "
        f"{out['wiped']['identities']} definition(s), {out['wiped']['feedback']} decision(s), "
        f"{out['wiped']['playlist_intents']} old-playlist description(s)."
    )
    for f in out["failed"]:
        print(
            f"  ! couldn't remove {f['spotify_id']}: {f['error']} (delete it in Spotify; "
            "the app will ignore it either way)"
        )
    print(
        "Start your new collection:\n"
        '  curator describe "chill, mellow, laid back" --like "Lavender Buds" --create "Chill"\n'
        "  curator draft"
    )
    return 0 if not out["failed"] else 2


def register(sub: Any) -> None:
    p = sub.add_parser("sync", help="preview (and with --apply, write) your playlists to Spotify")
    p.add_argument("--apply", action="store_true", help="write the previewed changes")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.add_argument("--only", action="append", metavar="NAME", help="limit to these playlists")
    p.add_argument("--songs", action="store_true", help="list every song added/removed")
    p.set_defaults(func=cmd_sync)
    ssub = p.add_subparsers(dest="sync_command")
    ssub.add_parser("local-files", help="local files to add by hand").set_defaults(
        func=cmd_local_files
    )
    ssub.add_parser("managed", help="playlists the app manages in Spotify").set_defaults(
        func=cmd_managed
    )
    q = ssub.add_parser("history", help="saved states of one playlist")
    q.add_argument("playlist")
    q.set_defaults(func=cmd_history)
    q = ssub.add_parser("release", help="stop syncing a playlist (it stays in Spotify)")
    q.add_argument("playlist")
    q.set_defaults(func=cmd_release)

    p = sub.add_parser(
        "start-over", help="remove the app's playlists from Spotify and your playlist definitions"
    )
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_start_over)

    p = sub.add_parser("rollback", help="restore a managed playlist to a saved state")
    p.add_argument("playlist")
    p.add_argument("--to", type=int, metavar="SNAPSHOT_ID", help="default: before the last sync")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_rollback)
