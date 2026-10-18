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
from playlist_curator.crate.importer import AccountMismatchError, import_library
from playlist_curator.crate.inventory import playlist_table, summarize
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
    s = summarize(db)
    lines = [
        ("Unique tracks in crate", s.unique_tracks),
        ("  Spotify catalog tracks", s.catalog_tracks),
        ("  Local-file entries", s.local_tracks),
        ("In Liked Songs", s.in_liked),
        ("In at least one playlist", s.in_playlists),
        ("In both", s.in_both),
        ("Only in Liked Songs", s.liked_only),
        ("Only in playlists (not liked)", s.playlists_only),
        ("Repeated entries inside a playlist", s.repeated_within_playlist),
        ("Same-ISRC groups (possible duplicates)", s.isrc_groups_with_multiple_uris),
        ("Catalog tracks without ISRC", s.catalog_missing_isrc),
        ("Playlists in library", s.playlists_total),
        ("  owned by you", s.playlists_owned),
        ("  contents imported", s.playlists_fetched),
        ("  contents unavailable via API", s.playlists_contents_unavailable),
        ("  errors", s.playlists_error),
    ]
    width = max(len(label) for label, _ in lines)
    for label, value in lines:
        print(f"{label:<{width}}  {value:>7}")
    if s.local_file_playlists:
        print(f"Playlists containing local files: {', '.join(s.local_file_playlists)}")
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
