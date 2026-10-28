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
from playlist_curator.metadata import vibes
from playlist_curator.metadata.descriptors import load_descriptors
from playlist_curator.metadata.vibes import load_classes
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
                classes=load_classes(db),
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
                if m.kind == "artist":
                    # an artist means "their songs that fit this", not every song they made
                    fit = set(ds.songs_that_fit(m.uris, recipe, db, descriptors))
                    pairs = [(u, lab) for u, lab in zip(m.uris, m.labels, strict=True) if u in fit]
                    if not pairs:
                        print(
                            f"    ~ {m.query}: none of their {len(m.uris)} song(s) fit the "
                            "description; name songs instead"
                        )
                        continue
                    note = f"artist, {len(pairs)} of {len(m.uris)} songs fit"
                    uris, labels = [u for u, _ in pairs], [lab for _, lab in pairs]
                else:
                    note = f"{m.kind}, {len(m.uris)} song(s)"
                    uris, labels = m.uris, m.labels
                example_uris += uris
                shown = "; ".join(labels[:4]) + (" ..." if len(labels) > 4 else "")
                print(f"    ✓ {m.query} ({note}): {shown}")
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

        if args.create or args.apply or args.redefine:
            try:
                iid = ds.apply_recipe(
                    db,
                    recipe,
                    create=args.create,
                    apply_to=args.apply or args.redefine,
                    example_uris=example_uris,
                    kind=args.kind,
                    replace=bool(args.redefine),
                )
            except (LookupError, ValueError) as exc:
                print(exc, file=sys.stderr)
                return 1
            ident = idn.get_identity(db, iid)
            verb = "Created" if args.create else ("Redefined" if args.redefine else "Updated")
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
        if args.coverage:
            sc.set_coverage(db, args.coverage == "on")
        if args.style:
            sc.set_style(db, args.style)
        if not idn.list_identities(db):
            print('No playlists defined yet. Make one: curator describe "..." --create "Name"')
            return 1
        res = sc.build_drafts(db)
        cov = sc.coverage_enabled(db)
        print(f"Style: {sc.draft_style(db)} (change with --style balanced or strict)")
    total = len(res.unassigned) + len({s.recording_id for d in res.drafts for s in d.include})
    print(
        f"Draft #{res.run_id}: {len(res.drafts)} playlists from {total} songs. "
        "Nothing was sent to Spotify.\n"
    )
    print(f"  {'playlist':<28} {'songs':>5} {'weak':>5} {'review':>6}  threshold")
    for d in sorted(res.drafts, key=lambda x: x.identity.name.lower()):
        th = f"{d.threshold:.2f}" if d.threshold is not None else "hand-picked"
        print(
            f"  {d.identity.name[:28]:<28} {len(d.include):>5} {d.weak:>5} {len(d.review):>6}  {th}"
        )
        for n in d.notes:
            if not n.startswith("hand-picked"):
                print(f"      · {n}")
    print(
        "\n  'weak' = songs that cleared no playlist's bar and went to their closest match, so "
        "every song has a home"
        if cov
        else "\n  Every-song-somewhere is off (--coverage on)."
    )
    print(
        f"  Songs in no playlist: {len(res.unassigned)}"
        f" ({res.untagged} have no data on the song at all, so they can't be judged;"
        ' add them by hand: curator feedback "Name" essential "Song")'
    )
    moved = {k: v for k, v in res.changes.items() if v["added"] or v["removed"]}
    if moved:
        print(
            "  Changes since the last draft: "
            + ", ".join(f"{k} +{v['added']}/-{v['removed']}" for k, v in sorted(moved.items()))
        )
    for o in res.overlaps:
        print(
            f"  ~ {o['a']} and {o['b']} share {o['shared']} songs "
            f"({round(100 * o['share_of_smaller'])}% of the smaller): maybe redundant"
        )
    print(
        '\nLook inside one: curator drafts show "Name"   ·   leftovers: curator drafts unassigned'
    )
    print('Decide song by song: curator review "Name"   ·   progress: curator stats')
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


# --- tags -----------------------------------------------------------------------------------


def cmd_tags(settings: Settings, args: argparse.Namespace) -> int:
    from collections import Counter

    db = _db(settings)
    if db is None:
        return 1
    with db:
        classes = load_classes(db)
        counts: Counter = Counter()
        for d in load_descriptors(db).values():
            seen = set()
            for src in vibes.SONG_SOURCES:
                for t, w in (d.by_source.get(src) or {}).items():
                    if w >= 0.3:
                        seen.add(t)
            counts.update(seen)
    by_class: dict[str, list[tuple[str, int]]] = {}
    for t, n in counts.most_common():
        by_class.setdefault(classes.of(t), []).append((t, n))
    print(
        "Tags on your songs themselves, by how they're read. vibe = always counts; genre / era "
        "= only in playlists that ask for them; other = unrecognized, ignored until you "
        "promote it; ignore = never.\n"
    )
    for cls in [args.cls] if args.cls else ["vibe", "other", "genre", "era", "ignore"]:
        items = by_class.get(cls, [])
        print(f"{cls} ({len(items)} tags):")
        line = ", ".join(
            f"{t}{'*' if t in classes.overrides else ''} ({n})" for t, n in items[: args.top]
        )
        print(f"  {line or '-'}\n")
    print('* = your choice. Change one: curator tags set "indie" vibe   (or ... default)')
    return 0


def cmd_tags_set(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        vibes.set_class(db, args.tag, None if args.cls == "default" else args.cls)
        now = load_classes(db).of(args.tag.strip().lower())
    print(f"'{args.tag}' is now read as: {now}. Rebuild drafts to use it: curator draft")
    return 0


def cmd_tags_songs(settings: Settings, args: argparse.Namespace) -> int:
    """Every song with its own descriptors, so you can see how the library reads."""
    import json as _json

    db = _db(settings)
    if db is None:
        return 1
    with db:
        desc = load_descriptors(db)
        classes = load_classes(db)
        rows = db.conn.execute(
            "SELECT recording_id, title, artists_json FROM recordings ORDER BY title"
        ).fetchall()
        only: set[str] | None = None
        if args.playlist:
            run = sc.latest_run(db)
            ident = idn.get_identity(db, args.playlist)
            only = {
                r[0]
                for r in db.conn.execute(
                    "SELECT recording_id FROM draft_members WHERE run_id=? AND identity_id=? "
                    "AND decision='include'",
                    (run, ident.identity_id),
                )
            }
    shown = counts = 0
    by_evidence: dict[str, int] = {}
    for r in rows:
        if only is not None and r["recording_id"] not in only:
            continue
        d = desc.get(r["recording_id"])
        prof = vibes.profile(d.by_source if d else {}, classes)
        by_evidence[prof.evidence] = by_evidence.get(prof.evidence, 0) + 1
        counts += len(prof.vibe)
        if shown >= args.limit:
            continue
        shown += 1
        top = [t for t, _ in sorted(prof.vibe.items(), key=lambda kv: -kv[1])[: args.words]]
        flag = {"artist": "  (artist only)", "none": "  (no data)"}.get(prof.evidence, "")
        artists = ", ".join(_json.loads(r["artists_json"]))
        print(f"{r['title'][:34]:<34} {artists[:20]:<20} {', '.join(top) or '-'}{flag}")
    n = sum(by_evidence.values())
    print(
        f"\n{n} songs: {by_evidence.get('song', 0)} judged by their own data, "
        f"{by_evidence.get('artist', 0)} by artist only, {by_evidence.get('none', 0)} with no data."
        f" Average {counts / max(n, 1):.1f} descriptors per song."
    )
    if shown < n:
        print(f"Showing {shown}; more with --limit {n}")
    return 0


def cmd_tags_song(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        rows = db.conn.execute(
            "SELECT recording_id, title, artists_json FROM recordings WHERE title LIKE ? "
            "ORDER BY title LIMIT 5",
            (f"%{args.title}%",),
        ).fetchall()
        if not rows:
            print(f"No song matches {args.title!r}")
            return 1
        desc = load_descriptors(db)
        classes = load_classes(db)
        for r in rows:
            d = desc.get(r["recording_id"])
            prof = vibes.profile(d.by_source if d else {}, classes)
            print(f"{r['title']} — {', '.join(__import__('json').loads(r['artists_json']))}")
            print(f"  judged by: {prof.evidence}")
            top = sorted(prof.vibe.items(), key=lambda kv: -kv[1])[:12]
            print("  sounds:   " + (", ".join(f"{t} {w:.2f}" for t, w in top) or "-"))
            labs = sorted(prof.labels.items(), key=lambda kv: -kv[1])[:8]
            print(
                "  labels:   "
                + (", ".join(f"{t} {w:.2f}" for t, w in labs) or "-")
                + "   (only count in playlists that ask for them)"
            )
            if d and d.facts.get("ab_features"):
                print(f"  audio:    {d.facts['ab_features']}  (AcousticBrainz)")
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
    target.add_argument(
        "--redefine",
        metavar="NAME",
        help="replace an existing playlist's description (keeps your examples and decisions)",
    )
    p.add_argument("--kind", choices=it.KINDS, default="mood")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("draft", help="fill your playlists with matching songs (drafts)")
    p.add_argument(
        "--coverage",
        choices=["on", "off"],
        help="try to give every song a home (default off; saved)",
    )
    p.add_argument(
        "--style",
        choices=list(sc.STYLES),
        help="how generous playlists are: strict (default), balanced, inclusive (saved)",
    )
    p.set_defaults(func=cmd_draft)

    t = sub.add_parser("tags", help="how tags are read: vibe / genre / era / ignore")
    t.add_argument("--class", dest="cls", choices=[*vibes.CLASSES, "other"])
    t.add_argument("--top", type=int, default=40)
    t.set_defaults(func=cmd_tags)
    ts = t.add_subparsers(dest="tags_command")
    q = ts.add_parser("set", help="reclassify a tag (your choice wins)")
    q.add_argument("tag")
    q.add_argument("cls", choices=[*vibes.CLASSES, "default"])
    q.set_defaults(func=cmd_tags_set)
    q = ts.add_parser("songs", help="every song with its own descriptors")
    q.add_argument("--playlist", help="only songs in this playlist's latest draft")
    q.add_argument("--limit", type=int, default=60)
    q.add_argument("--words", type=int, default=8, help="descriptors per song")
    q.set_defaults(func=cmd_tags_songs)
    q = ts.add_parser("song", help="how one song is judged (its own vibe, labels, source)")
    q.add_argument("title")
    q.set_defaults(func=cmd_tags_song)

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
