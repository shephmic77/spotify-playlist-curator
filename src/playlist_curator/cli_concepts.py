"""CLI commands for Phase 3b: descriptions of old playlists, proposals, playlist identities."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from playlist_curator.config import Settings
from playlist_curator.crate import deduplication as dedup
from playlist_curator.curator import discovery as disc
from playlist_curator.curator import identities as idn
from playlist_curator.curator import intents as it
from playlist_curator.storage.database import Database


def _db(settings: Settings) -> Database | None:
    if not settings.db_path.exists():
        print("No data yet. Run: curator import")
        return None
    db = Database(settings.db_path)
    dedup.ensure_current(db)
    return db


def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


# --- intents ---------------------------------------------------------------------------


def cmd_intents_list(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        intents = {i.playlist_id: i for i in it.list_intents(db)}
        rows = db.list_playlists()
    for r in rows:
        if r["contents_status"] != "fetched":
            continue
        i = intents.get(r["spotify_id"])
        desc = f"{i.kind:<10} {i.title} — {i.intent}" if i else "(no description)"
        print(f"{r['name'][:24]:<24} {r['stored_entries']:>4} songs  {desc}")
    print(
        "\nKinds: sound, mood, situation, era (can become concepts); handpicked (optional, "
        "never auto-filled); source, catch_all, mixed, unknown (songs just stay in the pool)."
    )
    return 0


def cmd_intents_set(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            i = it.set_intent(db, args.playlist, args.description, args.kind, args.title or "")
        except (it.PlaylistNotFound, it.AmbiguousPlaylist, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 1
    print(f"{i.playlist_name}: {i.kind} — {i.title}")
    return 0


def cmd_intents_import(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        applied, problems = it.import_intents(db, Path(args.file))
    print(f"Saved {len(applied)} description(s).")
    for p in problems:
        print(f"  ! {p}")
    return 0 if not problems else 2


# --- proposals ---------------------------------------------------------------------------


def _print_proposal(p: disc.Proposal, *, verbose: bool) -> None:
    label = {
        "from_description": "from your descriptions",
        "from_library": "found in your library",
        "handpicked": "hand-picked (optional)",
    }[p.action]
    ev = p.evidence
    head = f"[{p.proposal_id}] {p.title}  ({label}"
    if p.status != "pending":
        head += f", {p.status}"
    print(head + ")")
    print(f"     {p.draft.concept}")
    if p.action != "from_library":
        srcs = ", ".join(f"{x['name']} ({x['songs']})" for x in ev.get("from_playlists", []))
        print(f"     from old playlists: {srcs}; {ev.get('example_songs', 0)} example songs")
    else:
        print(f"     {ev['cluster_songs']} songs in your library cluster here")
    if ev.get("top_tags"):
        tags = ", ".join(
            f"{t['tag']} ({_pct(t['support'])}, ×{t['lift']})" for t in ev["top_tags"][:6]
        )
        print(f"     sound: {tags}")
    if p.draft.rules.get("years"):
        lo, hi = p.draft.rules["years"]
        print(f"     era from your description: {lo}–{hi} (preferred, not a hard limit)")
    elif ev.get("years"):
        y = ev["years"]
        print(f"     era: median {y['median']} ({y['p10']}–{y['p90']})")
    if not p.draft.auto_maintain:
        print("     the app will never add songs to this one automatically")
    for s in ev.get("similar_to", []):
        print(f"     ~ sounds like '{s['title']}' ({s['sim']}): you may only want one")
    if verbose:
        for c in ev.get("library_clusters", []):
            print(f"     + your library has {c['songs']} songs like this ({', '.join(c['tags'])})")
        for x in ev.get("from_playlists", []):
            if x.get("misfits_left_out"):
                n, name = x["misfits_left_out"], x["name"]
                print(f"     - {n} misfit(s) from {name} not used as examples")


def cmd_propose(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        if not it.list_intents(db):
            print("Tip: describe your old playlists first so proposals use your own concepts:")
            print(
                '  curator intents set "W" "songs for working out" --kind situation --title Workout'
            )
            print("Continuing with library clusters only.\n")
        props, info = disc.propose(db)
        saved = disc.save_proposals(db, props)
    print(
        f"{len(saved)} proposal(s) for a new collection. "
        f"Library clustering: {info.get('k', '-')} groups"
        + (f", silhouette {info['silhouette']}" if "silhouette" in info else "")
        + (f" ({info['skipped']})" if "skipped" in info else "")
    )
    print(
        "Review with: curator proposals   ·   accept: curator proposals accept ID [--name NAME]\n"
    )
    for p in saved:
        _print_proposal(p, verbose=False)
        print()
    return 0


def cmd_proposals_list(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        props = disc.load_proposals(db, include_decided=args.all)
    if not props:
        print("No proposals. Run: curator propose")
        return 0
    for p in props:
        _print_proposal(p, verbose=True)
        print()
    return 0


def cmd_proposals_accept(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        done = []
        for pid in args.ids:
            try:
                iid = disc.accept(db, int(pid), name=args.name if len(args.ids) == 1 else None)
                done.append((pid, idn.get_identity(db, iid)))
            except (KeyError, ValueError) as exc:
                print(f"  ! {exc}", file=sys.stderr)
    for pid, ident in done:
        print(f"Accepted [{pid}] → playlist #{ident.identity_id} '{ident.name}'")
    return 0 if len(done) == len(args.ids) else 1


def cmd_proposals_reject(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        for pid in args.ids:
            try:
                disc.reject(db, int(pid))
                print(f"Rejected [{pid}]")
            except KeyError as exc:
                print(f"  ! {exc.args[0]}", file=sys.stderr)
    return 0


def cmd_proposals_reopen(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        disc.reopen(db, int(args.id))
    print(f"[{args.id}] is pending again.")
    return 0


# --- identities -----------------------------------------------------------------------------


def _print_identity(i: idn.Identity, examples: list[dict[str, Any]] | None = None) -> None:
    flags = [] if i.auto_maintain else ["hand-picked"]
    if i.status != "approved":
        flags.append(i.status)
    extra = f"  [{', '.join(flags)}]" if flags else ""
    print(f"#{i.identity_id} {i.name}  ({i.kind}){extra}")
    if i.concept:
        print(f"    {i.concept}")
    tags = i.rules.get("tags") or {}
    if tags:
        print(
            "    tags: "
            + ", ".join(f"{t}={w:g}" for t, w in sorted(tags.items(), key=lambda x: -x[1]))
        )
    if i.rules.get("avoid_tags"):
        print("    avoid: " + ", ".join(i.rules["avoid_tags"]))
    if i.rules.get("years"):
        print(f"    years (preferred): {i.rules['years'][0]}–{i.rules['years'][1]}")
    ex = ", ".join(f"{n} {role}" for role, n in sorted(i.examples.items())) or "none"
    print(f"    example songs: {ex}")
    for e in examples or []:
        print(f"      · {e['title']} — {e['artists']} ({e['role']})")


def cmd_identities_list(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        items = idn.list_identities(db, include_inactive=args.all)
    if not items:
        print("No playlists defined yet. Accept proposals with: curator proposals accept ID")
    for i in items:
        _print_identity(i)
    return 0


def cmd_identities_show(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            i = idn.get_identity(db, args.ref)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        _print_identity(i, idn.example_songs(db, i.identity_id, limit=args.examples))
    return 0


def cmd_identities_edit(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            i = idn.edit_identity(
                db,
                args.ref,
                name=args.name,
                concept=args.concept,
                kind=args.kind,
                add_tags=idn.parse_tag_weights(args.add_tag or []),
                remove_tags=args.remove_tag,
                avoid_tags=args.avoid_tag,
                unavoid_tags=args.unavoid_tag,
                years=idn.parse_years(args.years) if args.years is not None else "keep",
                auto_maintain=args.auto,
            )
        except (LookupError, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 1
        _print_identity(i)
    return 0


def cmd_identities_status(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            i = idn.set_status(db, args.ref, args.status)
        except (LookupError, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 1
    print(f"#{i.identity_id} {i.name} is now {i.status}.")
    return 0


def register(sub: Any) -> None:
    intents = sub.add_parser("intents", help="describe what your old playlists were for")
    isub = intents.add_subparsers(dest="intents_command", required=True)
    isub.add_parser("list", help="show descriptions").set_defaults(func=cmd_intents_list)
    p = isub.add_parser("set", help="describe one old playlist")
    p.add_argument("playlist", help="name or id (use the id when two share a name)")
    p.add_argument("description")
    p.add_argument("--kind", required=True, choices=it.KINDS)
    p.add_argument("--title", help="short name for the concept, e.g. 'Workout'")
    p.set_defaults(func=cmd_intents_set)
    p = isub.add_parser("import", help="load descriptions from a JSON file")
    p.add_argument("file")
    p.set_defaults(func=cmd_intents_import)

    sub.add_parser("propose", help="propose a new playlist collection").set_defaults(
        func=cmd_propose
    )

    props = sub.add_parser("proposals", help="review proposals")
    psub = props.add_subparsers(dest="proposals_command")
    props.add_argument("--all", action="store_true", help="include accepted/rejected")
    props.set_defaults(func=cmd_proposals_list)
    p = psub.add_parser("accept", help="accept proposal(s) as playlists")
    p.add_argument("ids", nargs="+")
    p.add_argument("--name", help="rename (only when accepting one)")
    p.set_defaults(func=cmd_proposals_accept)
    p = psub.add_parser("reject", help="reject proposal(s)")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_proposals_reject)
    p = psub.add_parser("reopen", help="undo a rejection")
    p.add_argument("id")
    p.set_defaults(func=cmd_proposals_reopen)

    ids = sub.add_parser("identities", help="your new playlists' definitions")
    ids.add_argument("--all", action="store_true", help="include retired/rejected")
    ids.set_defaults(func=cmd_identities_list)
    dsub = ids.add_subparsers(dest="identities_command")
    p = dsub.add_parser("show", help="details and example songs")
    p.add_argument("ref", help="id or name")
    p.add_argument("--examples", type=int, default=15)
    p.set_defaults(func=cmd_identities_show)
    p = dsub.add_parser("edit", help="change name, concept, tags, years")
    p.add_argument("ref")
    p.add_argument("--name")
    p.add_argument("--concept")
    p.add_argument("--kind", choices=it.KINDS)
    p.add_argument("--add-tag", action="append", metavar="TAG[=WEIGHT]")
    p.add_argument("--remove-tag", action="append", metavar="TAG")
    p.add_argument("--avoid-tag", action="append", metavar="TAG")
    p.add_argument("--unavoid-tag", action="append", metavar="TAG")
    p.add_argument("--years", metavar="FROM-TO|none")
    auto = p.add_mutually_exclusive_group()
    auto.add_argument(
        "--auto",
        dest="auto",
        action="store_true",
        default=None,
        help="let the app add matching songs",
    )
    auto.add_argument(
        "--handpicked", dest="auto", action="store_false", help="never add songs automatically"
    )
    p.set_defaults(func=cmd_identities_edit)
    p = dsub.add_parser("status", help="approve, reject or retire")
    p.add_argument("ref")
    p.add_argument("status", choices=idn.STATUSES)
    p.set_defaults(func=cmd_identities_status)
