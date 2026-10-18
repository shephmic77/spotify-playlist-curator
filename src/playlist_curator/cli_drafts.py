"""CLI for describing playlists in your own words, and for Phase 4 draft playlists."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from playlist_curator.config import Settings
from playlist_curator.crate import deduplication as dedup
from playlist_curator.curator import describe as ds
from playlist_curator.curator import identities as idn
from playlist_curator.curator import intents as it
from playlist_curator.curator import scoring as sc
from playlist_curator.metadata.descriptors import load_descriptors
from playlist_curator.storage.database import Database


def _db(settings: Settings) -> Database | None:
    if not settings.db_path.exists():
        print("No data yet. Run: curator import")
        return None
    db = Database(settings.db_path)
    dedup.ensure_current(db)
    return db


# --- describe -------------------------------------------------------------------------------


def cmd_describe(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            recipe = ds.translate(
                args.text,
                engine=args.engine,
                api_key=settings.anthropic_api_key,
                model=settings.anthropic_model,
            )
        except ds.DescribeError as exc:
            print(exc, file=sys.stderr)
            return 1
        descriptors = load_descriptors(db)

        print(f'"{recipe.text}"')
        print(
            f"  engine: {recipe.engine}"
            + (f"  (matched: {', '.join(recipe.matched_terms)})" if recipe.matched_terms else "")
        )
        if recipe.note:
            print(f"  note: {recipe.note}")

        example_uris: list[str] = []
        if args.like:
            print("  your examples (matched in your library only):")
            for m in ds.resolve_examples(db, args.like):
                if m.kind == "none":
                    print(f"    ✗ {m.query}: not found in your library")
                    continue
                example_uris += m.uris
                shown = "; ".join(m.labels[:4]) + (" ..." if len(m.labels) > 4 else "")
                print(f"    ✓ {m.query} ({m.kind}, {len(m.uris)} song(s)): {shown}")
            shared = ds.example_tags(example_uris, db, descriptors)
            if shared:
                added = []
                for tag, support in shared:
                    if tag not in recipe.tags and tag not in recipe.avoid:
                        recipe.tags[tag] = round(0.8 * support, 2)
                        added.append(f"{tag} ({round(100 * support)}% of examples)")
                if added:
                    print("  added from what your examples have in common: " + ", ".join(added))

        print("  tags (how many of your songs carry each):")
        for r in ds.ground(recipe, descriptors):
            line = f"    {r.tag:<22} weight {r.weight:<4g} songs {r.songs:>4}"
            if r.suggestions:
                line += "   closest in your library: " + ", ".join(
                    f"{t} ({n})" for t, n in r.suggestions
                )
            print(line)
        if recipe.avoid:
            print(f"  avoid: {', '.join(recipe.avoid)}")
        if recipe.years:
            print(f"  preferred years: {recipe.years[0]}–{recipe.years[1]}")

        if args.create or args.apply:
            try:
                iid = ds.apply_recipe(
                    db,
                    recipe,
                    create=args.create,
                    apply_to=args.apply,
                    example_uris=example_uris,
                    kind=args.kind,
                )
            except (LookupError, ValueError) as exc:
                print(exc, file=sys.stderr)
                return 1
            ident = idn.get_identity(db, iid)
            verb = "Created" if args.create else "Updated"
            print(
                f"\n{verb} playlist #{ident.identity_id} '{ident.name}'. "
                "Run `curator draft` to fill it."
            )
        else:
            print('\nLooks right? Add --create "Name" (new playlist) or --apply "Name" (existing).')
    return 0


# --- drafts --------------------------------------------------------------------------------


def cmd_draft(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        if not idn.list_identities(db):
            print("No playlists defined yet. Accept proposals or use `curator describe`.")
            return 1
        res = sc.build_drafts(db)
    total = len(res.unassigned) + len({s.recording_id for d in res.drafts for s in d.include})
    print(
        f"Draft #{res.run_id}: {len(res.drafts)} playlists from {total} songs. "
        "Nothing was sent to Spotify.\n"
    )
    print(f"  {'playlist':<28} {'songs':>5} {'review':>6}  threshold")
    for d in sorted(res.drafts, key=lambda x: x.identity.name.lower()):
        th = f"{d.threshold:.2f}" if d.threshold is not None else "hand-picked"
        print(f"  {d.identity.name[:28]:<28} {len(d.include):>5} {len(d.review):>6}  {th}")
        for n in d.notes:
            if not n.startswith("hand-picked"):
                print(f"      · {n}")
    print(
        f"\n  Songs in no playlist: {len(res.unassigned)}"
        f" ({res.untagged} have no tags at all, so they can't be matched)"
    )
    for o in res.overlaps:
        print(
            f"  ~ {o['a']} and {o['b']} share {o['shared']} songs "
            f"({round(100 * o['share_of_smaller'])}% of the smaller): maybe redundant"
        )
    print(
        '\nLook inside one: curator drafts show "Name"   ·   leftovers: curator drafts unassigned'
    )
    return 0


def _row(r: dict[str, Any], *, reasons: bool) -> str:
    ev = {"song": "", "artist": " [artist tags only]", "none": " [no tags]"}[r["evidence"]]
    line = f"  {r['score']:.2f}  {r['title']} — {r['artists']}{ev}"
    if reasons and r["reasons"]:
        line += "\n          " + "; ".join(r["reasons"])
    return line


def cmd_drafts_summary(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        run = sc.latest_run(db)
        if run is None:
            print("No drafts yet. Run: curator draft")
            return 1
        for ident in idn.list_identities(db):
            rows = sc.load_draft(db, ident.identity_id, run)
            inc = [r for r in rows if r["decision"] == "include"]
            print(
                f"{ident.name} ({len(inc)} songs): "
                + ", ".join(r["title"] for r in inc[:5])
                + (" ..." if len(inc) > 5 else "")
            )
    return 0


def cmd_drafts_show(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            ident = idn.get_identity(db, args.ref)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        rows = sc.load_draft(db, ident.identity_id)
    if not rows:
        print("No draft for this playlist. Run: curator draft")
        return 1
    inc = [r for r in rows if r["decision"] == "include"]
    rev = [r for r in rows if r["decision"] == "review"]
    print(
        f"{ident.name}: {len(inc)} songs, {len(rev)} to review. "
        "Scores are for ranking, not probabilities.\n"
    )
    if not args.review_only:
        for r in inc[: args.limit]:
            print(_row(r, reasons=args.why))
        if len(inc) > args.limit:
            print(f"  ... {len(inc) - args.limit} more (use --limit)")
    if rev:
        print("\nBorderline (review):")
        for r in rev[: args.limit]:
            print(_row(r, reasons=True))
    return 0


def cmd_drafts_unassigned(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        run = sc.latest_run(db)
        if run is None:
            print("No drafts yet. Run: curator draft")
            return 1
        rows = db.conn.execute(
            """SELECT r.title, r.artists_json FROM recordings r
               WHERE r.recording_id NOT IN (
                   SELECT recording_id FROM draft_members WHERE run_id=? AND decision='include')
               ORDER BY r.title COLLATE NOCASE""",
            (run,),
        ).fetchall()
    import json

    print(
        f"{len(rows)} songs aren't in any draft playlist. That's allowed: a song only goes "
        "where it fits.\n"
    )
    for r in rows[: args.limit]:
        print(f"  {r['title']} — {', '.join(json.loads(r['artists_json']))}")
    if len(rows) > args.limit:
        print(f"  ... {len(rows) - args.limit} more (use --limit)")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser("describe", help="turn your own words into a playlist definition")
    p.add_argument("text", help='e.g. "music to study to, chill, lyrics ok but not distracting"')
    p.add_argument(
        "--like",
        action="append",
        metavar="ARTIST_OR_SONG",
        help="example from your library (repeatable), e.g. --like Nujabes",
    )
    p.add_argument(
        "--engine",
        choices=["auto", "offline", "claude"],
        default="auto",
        help="auto = Claude if ANTHROPIC_API_KEY is set, else built-in dictionary",
    )
    target = p.add_mutually_exclusive_group()
    target.add_argument("--create", metavar="NAME", help="create a new playlist from this")
    target.add_argument("--apply", metavar="NAME", help="add this to an existing playlist")
    p.add_argument("--kind", choices=it.KINDS, default="mood")
    p.set_defaults(func=cmd_describe)

    sub.add_parser("draft", help="fill your playlists with matching songs (drafts)").set_defaults(
        func=cmd_draft
    )

    d = sub.add_parser("drafts", help="look at the latest draft playlists")
    d.set_defaults(func=cmd_drafts_summary)
    dsub = d.add_subparsers(dest="drafts_command")
    p = dsub.add_parser("show", help="songs in one draft playlist, with reasons")
    p.add_argument("ref", help="playlist name or id")
    p.add_argument("--why", action="store_true", help="show the reason for every song")
    p.add_argument("--review-only", action="store_true")
    p.add_argument("--limit", type=int, default=60)
    p.set_defaults(func=cmd_drafts_show)
    p = dsub.add_parser("unassigned", help="songs that didn't fit any playlist")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_drafts_unassigned)
