"""Command-line entry point: ``curator <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from playlist_curator.config import ConfigError, Settings, configure_logging, load_settings
from playlist_curator.crate import deduplication as dedup
from playlist_curator.crate.importer import AccountMismatchError, import_library
from playlist_curator.crate.inventory import (
    decisions_table,
    duplicate_groups,
    local_files_table,
    pending_suggestions,
    playlist_table,
    summarize,
)
from playlist_curator.spotify.auth import AuthError, TokenManager, TokenStore, login
from playlist_curator.spotify.client import SpotifyAPIError, SpotifyClient
from playlist_curator.storage.database import Database, purge_data

log = logging.getLogger("playlist_curator")


def _client(settings: Settings) -> SpotifyClient:
    settings.validate()
    return SpotifyClient(TokenManager(settings))


def cmd_auth_login(settings: Settings, args: argparse.Namespace) -> int:
    login(settings, manual=args.manual)
    client = _client(settings)
    me = client.me()
    print(f"\nLogged in as {me.get('display_name') or me['id']} (user id {me['id']}).")
    print(f"Tokens saved to {settings.token_path}")
    return 0


def cmd_auth_status(settings: Settings, args: argparse.Namespace) -> int:
    tokens = TokenStore(settings.token_path).load()
    if tokens is None:
        print("Not logged in. Run: curator auth login")
        return 1
    me = _client(settings).me()
    tokens = TokenStore(settings.token_path).load() or tokens  # may have been refreshed
    exp = datetime.fromtimestamp(tokens.expires_at, UTC)
    print(f"Logged in as {me.get('display_name') or me['id']} (user id {me['id']})")
    print(f"Granted scopes: {tokens.scope or '(not reported)'}")
    print(f"Access token valid until {exp:%Y-%m-%d %H:%M} UTC (refreshed automatically)")
    return 0


def cmd_auth_logout(settings: Settings, args: argparse.Namespace) -> int:
    removed = TokenStore(settings.token_path).clear()
    print("Saved tokens deleted." if removed else "No saved tokens found.")
    print("To fully revoke access, remove the app at https://www.spotify.com/account/apps/")
    return 0


def cmd_import(settings: Settings, args: argparse.Namespace) -> int:
    client = _client(settings)
    with Database(settings.db_path) as db:
        result = import_library(client, db, force=args.force, progress=print)
        c = result.counts
        print(
            f"\nImport {result.status}: {c.liked_entries} liked, {c.playlists_seen} playlists "
            f"({c.playlists_fetched} read, {c.playlists_unchanged} unchanged, "
            f"{c.playlists_contents_unavailable} contents unavailable), "
            f"{c.new_tracks} new / {c.removed_tracks} removed tracks. "
            f"{client.request_count} API requests."
        )
        if c.skipped:
            print("Skipped entries: " + ", ".join(f"{k}={v}" for k, v in c.skipped.items()))
        for err in result.errors:
            print(f"  ERROR {err.get('name', '')}: {err['error']}")
        print()
        _print_summary(db)
    return 0 if result.status == "ok" else 2


def _print_summary(db: Database) -> None:
    dedup.ensure_current(db)
    s = summarize(db)
    sections = [
        (
            "Songs (duplicates merged)",
            [
                ("Unique recordings", s.unique_recordings),
                ("In Liked Songs", s.rec_in_liked),
                ("In at least one playlist", s.rec_in_playlists),
                ("In both", s.rec_in_both),
                ("Only in Liked Songs", s.rec_liked_only),
                ("Only in playlists (not liked)", s.rec_playlists_only),
            ],
        ),
        (
            "Duplicates",
            [
                ("Spotify URIs / local entries", s.unique_tracks),
                ("Recordings with several copies", s.merged_groups),
                ("Extra copies folded in", s.duplicate_uris_merged),
                ("Suggestions waiting for review", s.pending_title_suggestions),
                ("Repeated entries inside a playlist", s.repeated_within_playlist),
            ],
        ),
        (
            "Local files",
            [
                ("Local-file entries", s.local_tracks),
                ("  matched to a catalog song", s.local_matched),
                ("  not matched", s.local_unmatched),
                ("  match suggestions to review", s.pending_local_suggestions),
            ],
        ),
        (
            "Missing metadata",
            [
                ("Catalog tracks without ISRC", s.catalog_missing_isrc),
                ("Catalog tracks without release date", s.missing_release_date),
                ("Entries without an artist", s.missing_artist),
                ("Entries without a duration", s.missing_duration),
            ],
        ),
        (
            "Playlists",
            [
                ("Playlists in library", s.playlists_total),
                ("  owned by you", s.playlists_owned),
                ("  contents imported", s.playlists_fetched),
                ("  contents unavailable via API", s.playlists_contents_unavailable),
                ("  errors", s.playlists_error),
            ],
        ),
    ]
    width = max(len(label) for _, rows in sections for label, _ in rows)
    for title, rows in sections:
        print(title)
        for label, value in rows:
            print(f"  {label:<{width}}  {value:>7}")
    if s.local_file_playlists:
        print(f"Playlists containing local files: {', '.join(s.local_file_playlists)}")
    pending = s.pending_title_suggestions + s.pending_local_suggestions
    if pending:
        print(f"\n{pending} possible duplicate(s) need a decision: curator duplicates review")
    if s.playlists_contents_unavailable:
        print(
            "\nNote: Spotify only returns tracks for playlists you own or collaborate on. "
            "To include a followed playlist, copy its songs into a playlist you own."
        )


def cmd_inventory(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.db_path.exists():
        print("No data yet. Run: curator import")
        return 1
    with Database(settings.db_path) as db:
        if args.json:
            dedup.ensure_current(db)
            print(json.dumps(summarize(db).as_dict(), indent=2))
        else:
            _print_summary(db)
    return 0


def cmd_playlists(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.db_path.exists():
        print("No data yet. Run: curator import")
        return 1
    with Database(settings.db_path) as db:
        rows = playlist_table(db)
    for r in rows:
        flag = {"fetched": " ", "unavailable": "x", "error": "!", "pending": "?"}.get(
            r["contents"], "?"
        )
        owner = "you" if r["owned"] else (r["owner"] or "?")
        print(f"[{flag}] {r['name'][:48]:<48} {r['imported_entries']:>5} items  owner: {owner}")
    print("\n[x] contents unavailable via API   [!] error on last import")
    return 0


def _open_db(settings: Settings) -> Database | None:
    if not settings.db_path.exists():
        print("No data yet. Run: curator import")
        return None
    db = Database(settings.db_path)
    dedup.ensure_current(db)
    return db


def _describe(t: dict) -> str:
    bits = [f"{t.get('name')} — {t.get('artists') or '?'}"]
    if t.get("local_file"):
        bits.append("[local file]")
    else:
        album = t.get("album") or "?"
        kind = f", {t['album_type']}" if t.get("album_type") else ""
        bits.append(f"[{album}{kind}, {t.get('release_date') or '?'}]")
    if t.get("duration_s"):
        m, sec = divmod(t["duration_s"], 60)
        bits.append(f"{m}:{sec:02d}")
    if t.get("isrc"):
        bits.append(f"ISRC {t['isrc']}")
    bits.append(f"in {t.get('times_in_sources', 0)} source(s)")
    return "  ".join(bits)


def cmd_dup_list(settings: Settings, args: argparse.Namespace) -> int:
    db = _open_db(settings)
    if db is None:
        return 1
    with db:
        groups = duplicate_groups(db)
    if not groups:
        print("No recordings with more than one copy.")
        return 0
    for g in groups:
        print(f"\n{g['title']} — {g['artists']}   ({len(g['members'])} copies)")
        for m in g["members"]:
            tag = "*" if m["primary"] else " "
            print(f"  {tag} [{m['evidence']:<8}] {_describe(m)}")
            if args.uris:
                print(f"      {m['uri']}")
    print(
        "\n* = copy used when writing playlists.  Evidence: isrc = same ISRC, "
        "accepted = you confirmed."
    )
    print("To separate a copy: curator duplicates split <uri>  (see --uris)")
    return 0


def _apply(db: Database, short: str, decision: str) -> None:
    a, b = dedup.resolve_suggestion(db, short)
    dedup.decide(db, a, b, decision)


def cmd_dup_review(settings: Settings, args: argparse.Namespace) -> int:
    db = _open_db(settings)
    if db is None:
        return 1
    with db:
        items = pending_suggestions(db, args.kind)
        if not items:
            print("Nothing to review.")
            return 0
        print(
            f"{len(items)} suggestion(s). For each: [s]ame recording, [d]ifferent, "
            "[k] skip, [q] quit.\n"
        )
        done = 0
        for i, item in enumerate(items, 1):
            print(f"({i}/{len(items)}) {item['kind']} · {item['strength']} · id {item['id']}")
            print(f"  {item['reason']}")
            print(f"  A: {_describe(item['a'])}")
            print(f"  B: {_describe(item['b'])}")
            while True:
                answer = input("  same / different / skip / quit? [s/d/k/q] ").strip().lower()
                if answer in ("s", "d", "k", "q"):
                    break
            if answer == "q":
                break
            if answer in ("s", "d"):
                decision = "same" if answer == "s" else "different"
                dedup.decide(db, item["a"]["uri"], item["b"]["uri"], decision)
                done += 1
            print()
        print(f"Saved {done} decision(s). Undo with: curator duplicates undo <id>")
    return 0


def cmd_dup_decide(settings: Settings, args: argparse.Namespace) -> int:
    db = _open_db(settings)
    if db is None:
        return 1
    with db:
        try:
            _apply(db, args.id, args.decision)
        except KeyError as exc:
            print(exc.args[0], file=sys.stderr)
            return 1
    meaning = "the same recording" if args.decision == "same" else "different recordings"
    print(f"Recorded: {args.id} is {meaning}.")
    return 0


def cmd_dup_split(settings: Settings, args: argparse.Namespace) -> int:
    db = _open_db(settings)
    if db is None:
        return 1
    with db:
        try:
            dedup.split_out(db, args.uri)
        except KeyError:
            print(f"Unknown URI: {args.uri}", file=sys.stderr)
            return 1
    print("Separated. It will no longer be grouped with the other copies.")
    return 0


def cmd_dup_decisions(settings: Settings, args: argparse.Namespace) -> int:
    db = _open_db(settings)
    if db is None:
        return 1
    with db:
        rows = decisions_table(db)
    if not rows:
        print("No decisions yet.")
    for r in rows:
        print(f"{r['id']}  {r['decision']:<9} {r['decided_at']}")
        print(f"    A: {_describe(r['a'])}")
        print(f"    B: {_describe(r['b'])}")
    return 0


def cmd_dup_undo(settings: Settings, args: argparse.Namespace) -> int:
    db = _open_db(settings)
    if db is None:
        return 1
    with db:
        keys = [r["pair_key"] for r in decisions_table(db) if r["id"].startswith(args.id)]
        if len(keys) != 1:
            print(
                "No unique decision with that id. See: curator duplicates decisions",
                file=sys.stderr,
            )
            return 1
        dedup.undo_decision(db, keys[0])
    print("Decision removed.")
    return 0


def cmd_local_files(settings: Settings, args: argparse.Namespace) -> int:
    db = _open_db(settings)
    if db is None:
        return 1
    with db:
        rows = local_files_table(db)
    if not rows:
        print("No local files found in your playlists.")
        return 0
    for r in rows:
        status = f"= {r['matched_recording']}" if r["matched_recording"] else "unmatched"
        if r["pending_suggestions"]:
            status += f" ({r['pending_suggestions']} suggestion(s) to review)"
        artist = f" — {r['artist']}" if r["artist"] else ""
        print(f"{r['title']}{artist}")
        print(f"    in: {', '.join(r['in_playlists']) or '-'}   {status}")
    print(
        "\nLocal files can't be added to playlists through the API. Later phases will list "
        "where each one belongs so you can add it in the desktop app."
    )
    return 0


def cmd_purge(settings: Settings, args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input(
            f"Delete the local database and saved tokens in {settings.data_dir}? "
            "Nothing in your Spotify account is changed. [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    removed = purge_data(settings.data_dir)
    print(f"Removed {len(removed)} file(s)." if removed else "Nothing to remove.")
    return 0


def cmd_ui(settings: Settings, args: argparse.Namespace) -> int:
    app = Path(__file__).with_name("app.py")
    try:
        return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)])
    except FileNotFoundError:
        print("Streamlit is not installed. Run: pip install -e '.[ui]'")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="curator", description="Personal Spotify curator")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="log in / out of Spotify").add_subparsers(
        dest="auth_command", required=True
    )
    p = auth.add_parser("login", help="authorize with Spotify (PKCE)")
    p.add_argument(
        "--manual", action="store_true", help="paste the redirect URL instead of a local server"
    )
    p.set_defaults(func=cmd_auth_login)
    auth.add_parser("status", help="show the logged-in account").set_defaults(func=cmd_auth_status)
    auth.add_parser("logout", help="delete saved tokens").set_defaults(func=cmd_auth_logout)

    p = sub.add_parser("import", help="read Liked Songs and playlists into the local crate")
    p.add_argument("--force", action="store_true", help="re-read playlists even if unchanged")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("inventory", help="summarize the imported library")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inventory)

    sub.add_parser("playlists", help="list imported playlists").set_defaults(func=cmd_playlists)

    p = sub.add_parser("purge", help="delete all local data and tokens")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_purge)

    dup = sub.add_parser("duplicates", help="review copies of the same recording").add_subparsers(
        dest="dup_command", required=True
    )
    p = dup.add_parser("list", help="recordings that have several copies")
    p.add_argument("--uris", action="store_true", help="show Spotify URIs")
    p.set_defaults(func=cmd_dup_list)
    p = dup.add_parser("review", help="go through pending suggestions interactively")
    p.add_argument("--kind", choices=["title_match", "local_match"])
    p.set_defaults(func=cmd_dup_review)
    for name, decision, text in (
        ("same", "same", "mark a suggestion as the same recording"),
        ("different", "different", "mark a suggestion as different recordings"),
    ):
        p = dup.add_parser(name, help=text)
        p.add_argument("id", help="suggestion id from 'duplicates review'")
        p.set_defaults(func=cmd_dup_decide, decision=decision)
    p = dup.add_parser("split", help="separate one copy from its group")
    p.add_argument("uri")
    p.set_defaults(func=cmd_dup_split)
    dup.add_parser("decisions", help="list your decisions").set_defaults(func=cmd_dup_decisions)
    p = dup.add_parser("undo", help="remove a decision")
    p.add_argument("id")
    p.set_defaults(func=cmd_dup_undo)

    sub.add_parser("local-files", help="local files and their matches").set_defaults(
        func=cmd_local_files
    )

    sub.add_parser("ui", help="open the Streamlit interface").set_defaults(func=cmd_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging("DEBUG" if args.verbose else settings.log_level)
    try:
        return int(args.func(settings, args) or 0)
    except (ConfigError, AuthError, AccountMismatchError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except SpotifyAPIError as exc:
        print(f"Spotify error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
