"""CLI for Phase 5: review drafts song by song, record decisions, see what was learned."""

from __future__ import annotations

import argparse
import contextlib
import sys
from typing import Any

from playlist_curator.config import Settings
from playlist_curator.crate import deduplication as dedup
from playlist_curator.curator import feedback as fb
from playlist_curator.curator import identities as idn
from playlist_curator.curator import scoring as sc
from playlist_curator.storage.database import Database


def _db(settings: Settings) -> Database | None:
    if not settings.db_path.exists():
        print("No data yet. Run: curator import")
        return None
    db = Database(settings.db_path)
    dedup.ensure_current(db)
    return db


KEYS = {"k": "keep", "r": "reject", "e": "essential", "u": "uncertain"}


def cmd_review(settings: Settings, args: argparse.Namespace) -> int:
    """Walk through a draft playlist: borderline songs first, then (optionally) included ones."""
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            ident = idn.get_identity(db, args.playlist)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        rows = sc.load_draft(db, ident.identity_id)
        if not rows:
            print("No draft yet. Run: curator draft")
            return 1
        decided = {r["uri"] for r in fb.decisions_for(db, ident.identity_id)}
        uri_of = {
            r[0]: r[1] for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings")
        }
        queue = [r for r in rows if r["decision"] == "review"]
        if args.all:
            queue += [r for r in rows if r["decision"] == "include"]
        queue = [r for r in queue if uri_of.get(r["recording_id"]) not in decided]
        if not queue:
            print("Nothing left to review here. Use --all to go through included songs too.")
            return 0
        print(
            f"{ident.name}: {len(queue)} song(s). "
            "[k]eep  [r]eject  [e]ssential  [u]nsure  [s]kip  [q]uit\n"
        )
        counts: dict[str, int] = {}
        for i, r in enumerate(queue, 1):
            tag = "borderline" if r["decision"] == "review" else "in draft"
            print(f"({i}/{len(queue)}) {r['title']} — {r['artists']}   [{tag}, {r['score']:.2f}]")
            if r["reasons"]:
                print("      " + "; ".join(r["reasons"]))
            while True:
                ans = input("      k/r/e/u/s/q? ").strip().lower()
                if ans in (*KEYS, "s", "q"):
                    break
            if ans == "q":
                break
            if ans in KEYS:
                fb.decide_uri(db, ident, uri_of[r["recording_id"]], KEYS[ans])
                counts[KEYS[ans]] = counts.get(KEYS[ans], 0) + 1
        done = ", ".join(f"{v} {k}" for k, v in counts.items()) or "no decisions"
        print(f"\nSaved: {done}. Run `curator draft` to rebuild with your choices.")
        _print_suggestions(db, ident.identity_id)
    return 0


def _print_suggestions(db: Database, identity_id: int) -> None:
    sugg = fb.suggest_avoid_tags(db, identity_id, sc.load_descriptors(db))
    if sugg:
        name = idn.get_identity(db, identity_id).name
        tags = ", ".join(
            f"{s['tag']} (on {round(100 * s['in_rejected'])}% of your rejects, "
            f"{round(100 * s['in_examples'])}% of examples)"
            for s in sugg
        )
        print(f"Pattern in what you rejected: {tags}")
        first = sugg[0]["tag"]
        print(f'  To avoid it: curator identities edit "{name}" --avoid-tag "{first}"')


def cmd_feedback(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        results = []
        for song in args.songs:
            try:
                if args.decision == "undo":
                    r = fb.undo(db, args.playlist, song)
                    results.append(f"undone: {r['song']} (back to: {r['restored']})")
                else:
                    r = fb.decide(db, args.playlist, song, args.decision)
                    results.append(f"{r['decision']}: {r['song']}  (draft had it: {r['was']})")
            except (fb.SongNotFound, fb.AmbiguousSong, LookupError, ValueError) as exc:
                results.append(f"! {exc}")
        for line in results:
            print(line)
        with contextlib.suppress(LookupError):
            _print_suggestions(db, idn.get_identity(db, args.playlist).identity_id)
    return 0 if not any(x.startswith("!") for x in results) else 1


def cmd_decisions(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        try:
            ident = idn.get_identity(db, args.playlist)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        rows = fb.decisions_for(db, ident.identity_id)
    if not rows:
        print(f"No decisions yet for {ident.name}.")
    for r in rows:
        print(f"  {r['decision']:<9} {r['song']}   (draft had it: {r['was']}, {r['decided_at']})")
    return 0


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{round(100 * x)}%"


def cmd_stats(settings: Settings, args: argparse.Namespace) -> int:
    db = _db(settings)
    if db is None:
        return 1
    with db:
        st = fb.stats(db)
        evals = {e.name: e for e in fb.evaluate(db)} if args.evaluate else {}
        run = sc.latest_run(db)
        changes: dict[str, Any] = {}
        if run:
            import json

            summary = db.conn.execute(
                "SELECT summary_json FROM draft_runs WHERE run_id=?", (run,)
            ).fetchone()
            changes = json.loads(summary[0]).get("changes", {})
    print(
        f"  {'playlist':<26} {'kept':>4} {'rej':>4} {'ess':>4} {'unsure':>6} "
        f"{'accept%':>7} {'manual':>6} {'+/- last draft':>14}"
        + ("  holdout agreement" if args.evaluate else "")
    )
    for s in st:
        ch = changes.get(s.name)
        chs = f"+{ch['added']}/-{ch['removed']}" if ch else "-"
        line = (
            f"  {s.name[:26]:<26} {s.kept:>4} {s.rejected:>4} {s.essential:>4} "
            f"{s.uncertain:>6} {_pct(s.acceptance_rate):>7} {s.manual_adds:>6} {chs:>14}"
        )
        e = evals.get(s.name)
        if e:
            line += f"  {_pct(e.agreement)} of {e.decided}"
        print(line)
    print(
        "\naccept% = of the songs the draft suggested and you judged, how many you kept. "
        "manual = songs you added that the draft hadn't suggested."
    )
    if args.evaluate:
        print(
            "holdout = each decision hidden in turn: how often the playlist would have agreed "
            "with you anyway. It's a check, not a probability."
        )
        for e in evals.values():
            for t in e.kept_but_would_drop[:3]:
                print(f"  {e.name}: you kept {t}, but the rules alone would drop it")
            for t in e.rejected_but_would_add[:3]:
                print(f"  {e.name}: you rejected {t}, but the rules alone would add it")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser("review", help="go through a draft playlist and decide song by song")
    p.add_argument("playlist")
    p.add_argument("--all", action="store_true", help="also review songs already in the draft")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("feedback", help="keep / reject / essential / uncertain / undo songs")
    p.add_argument("playlist")
    p.add_argument("decision", choices=[*fb.DECISIONS, "undo"])
    p.add_argument("songs", nargs="+", help='"Title", "Title - Artist", or a Spotify URI')
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("decisions", help="your decisions for one playlist")
    p.add_argument("playlist")
    p.set_defaults(func=cmd_decisions)

    p = sub.add_parser("stats", help="acceptance, rejections, changes between drafts")
    p.add_argument("--evaluate", action="store_true", help="also run the holdout check")
    p.set_defaults(func=cmd_stats)
