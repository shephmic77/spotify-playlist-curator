"""Phase 5: your decisions on songs, per playlist, and what the app learns from them.

Decisions (always for one playlist; a rejection in one playlist never affects another):
- **keep**      → the song becomes an example for that playlist (stays in, and shapes what
                  "sounds like this playlist" means)
- **reject**    → never in that playlist again; songs that sound more like your rejected songs
                  than like the playlist's examples are scored down there
- **essential** → always in, regardless of score
- **uncertain** → noted for metrics; changes nothing
- **undo**      → back to how it was before your decision

Manual decisions always beat automatic scores. Nothing is trained: decisions become example
songs, exclusions and suggested rules you can see and edit.

Learning is transparent:
- ``suggest_avoid_tags``: tags common among a playlist's rejected songs but rare among its
  examples (you choose whether to add them).
- ``evaluate``: a holdout check. Each decided song is re-scored as if you hadn't decided it;
  we count how often the playlist would have agreed with you.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from playlist_curator.curator.identities import Identity, get_identity, list_identities
from playlist_curator.metadata.descriptors import Descriptors
from playlist_curator.storage.database import Database, utcnow

DECISIONS = ("keep", "reject", "essential", "uncertain")
ROLE_FOR = {"keep": "reference", "reject": "exclude", "essential": "essential"}


class SongNotFound(LookupError):
    pass


class AmbiguousSong(LookupError):
    def __init__(self, query: str, labels: list[str]) -> None:
        self.labels = labels
        shown = "; ".join(labels[:6])
        super().__init__(f"{query!r} matches several songs: {shown}. Be more specific.")


# ---------------------------------------------------------------------------
# Finding songs
# ---------------------------------------------------------------------------


def find_song(db: Database, query: str, identity_id: int | None = None) -> tuple[str, str]:
    """(primary uri, label) for a song in your crate, by URI, "title", or "title - artist".

    When a playlist is given, songs in its latest draft are preferred, so "Numb" means the
    Numb that's in the Workout draft.
    """
    from playlist_curator.crate.deduplication import normalize_artist, parse_title

    rows = db.conn.execute("SELECT recording_id, primary_uri, title, artists_json FROM recordings")
    recs = [
        (r["recording_id"], r["primary_uri"], r["title"], json.loads(r["artists_json"]))
        for r in rows
    ]
    if query.startswith("spotify:"):
        rid = db.conn.execute(
            "SELECT recording_id FROM recording_members WHERE uri=?", (query,)
        ).fetchone()
        for r in recs:
            if rid and r[0] == rid[0]:
                return r[1], f"{r[2]} — {', '.join(r[3])}"
        raise SongNotFound(f"{query} isn't in your library")

    title_q, artist_q = query, None
    for sep in (" — ", " - ", " by "):
        if sep in query:
            title_q, artist_q = query.split(sep, 1)
            break
    base = parse_title(title_q).base
    hits = [r for r in recs if parse_title(r[2]).base == base]
    if not hits:
        hits = [r for r in recs if base and parse_title(r[2]).base.startswith(base)]
    if artist_q:
        a = normalize_artist(artist_q)
        hits = [r for r in hits if any(a in normalize_artist(x) for x in r[3])]
    if len(hits) > 1 and identity_id is not None:
        from playlist_curator.curator.scoring import latest_run

        run = latest_run(db)
        in_draft = {
            x[0]
            for x in db.conn.execute(
                "SELECT recording_id FROM draft_members WHERE run_id=? AND identity_id=?",
                (run, identity_id),
            )
        }
        narrowed = [r for r in hits if r[0] in in_draft]
        hits = narrowed or hits
    if not hits:
        raise SongNotFound(f"No song called {query!r} in your library")
    if len(hits) > 1:
        raise AmbiguousSong(query, [f"{r[2]} — {', '.join(r[3])}" for r in hits])
    r = hits[0]
    return r[1], f"{r[2]} — {', '.join(r[3])}"


# ---------------------------------------------------------------------------
# Recording decisions
# ---------------------------------------------------------------------------


def _draft_state(db: Database, identity_id: int, uri: str) -> tuple[str, float | None]:
    from playlist_curator.curator.scoring import latest_run

    run = latest_run(db)
    if run is None:
        return "none", None
    row = db.conn.execute(
        """SELECT d.decision, d.score FROM draft_members d
           JOIN recording_members rm ON rm.recording_id = d.recording_id
           WHERE d.run_id=? AND d.identity_id=? AND rm.uri=?""",
        (run, identity_id, uri),
    ).fetchone()
    return (row[0], row[1]) if row else ("none", None)


def decide(db: Database, playlist: str | int, song: str, decision: str) -> dict[str, Any]:
    """Record a decision for one song in one playlist. ``song`` is a title, "title - artist",
    or a Spotify URI from your library."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of: {', '.join(DECISIONS)}")
    ident = get_identity(db, playlist)
    uri, label = find_song(db, song, ident.identity_id)
    return decide_uri(db, ident, uri, decision, label=label)


def decide_uri(
    db: Database, ident: Identity, uri: str, decision: str, *, label: str = ""
) -> dict[str, Any]:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of: {', '.join(DECISIONS)}")
    state, score = _draft_state(db, ident.identity_id, uri)
    now = utcnow()
    existing = db.conn.execute(
        "SELECT prev_role, prev_source FROM feedback WHERE identity_id=? AND uri=?",
        (ident.identity_id, uri),
    ).fetchone()
    if existing is not None:  # re-deciding: keep the original pre-feedback state for undo
        prev_role, prev_source = existing["prev_role"], existing["prev_source"]
    else:
        ex = db.conn.execute(
            "SELECT role, source FROM identity_examples WHERE identity_id=? AND uri=?",
            (ident.identity_id, uri),
        ).fetchone()
        prev_role, prev_source = (ex["role"], ex["source"]) if ex else (None, None)
    with db.transaction():
        db.conn.execute(
            "DELETE FROM identity_examples WHERE identity_id=? AND uri=?", (ident.identity_id, uri)
        )
        role = ROLE_FOR.get(decision)
        if role:
            db.conn.execute(
                "INSERT INTO identity_examples VALUES (?,?,?,?,?)",
                (ident.identity_id, uri, role, "feedback", now),
            )
        elif prev_role:  # uncertain: leave the song as it was
            db.conn.execute(
                "INSERT INTO identity_examples VALUES (?,?,?,?,?)",
                (ident.identity_id, uri, prev_role, prev_source or "seed", now),
            )
        db.conn.execute(
            "INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?,?,?)",
            (ident.identity_id, uri, decision, prev_role, prev_source, score, state, now),
        )
        db.conn.execute(
            "INSERT INTO feedback_log (identity_id, uri, decision, draft_state, score, decided_at)"
            " VALUES (?,?,?,?,?,?)",
            (ident.identity_id, uri, decision, state, score, now),
        )
    return {"playlist": ident.name, "song": label or uri, "decision": decision, "was": state}


def undo(db: Database, playlist: str | int, song: str) -> dict[str, Any]:
    ident = get_identity(db, playlist)
    uri, label = find_song(db, song, ident.identity_id)
    row = db.conn.execute(
        "SELECT prev_role, prev_source FROM feedback WHERE identity_id=? AND uri=?",
        (ident.identity_id, uri),
    ).fetchone()
    if row is None:
        raise SongNotFound(f"No decision recorded for {label} in {ident.name}")
    with db.transaction():
        db.conn.execute(
            "DELETE FROM identity_examples WHERE identity_id=? AND uri=?", (ident.identity_id, uri)
        )
        if row["prev_role"]:
            db.conn.execute(
                "INSERT INTO identity_examples VALUES (?,?,?,?,?)",
                (ident.identity_id, uri, row["prev_role"], row["prev_source"] or "seed", utcnow()),
            )
        db.conn.execute(
            "DELETE FROM feedback WHERE identity_id=? AND uri=?", (ident.identity_id, uri)
        )
        db.conn.execute(
            "INSERT INTO feedback_log (identity_id, uri, decision, draft_state, score, decided_at)"
            " VALUES (?,?,'undo','none',NULL,?)",
            (ident.identity_id, uri, utcnow()),
        )
    return {"playlist": ident.name, "song": label, "restored": row["prev_role"] or "nothing"}


def decisions_for(db: Database, identity_id: int) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        """SELECT f.*, t.name, t.artists_json FROM feedback f JOIN tracks t ON t.uri = f.uri
           WHERE f.identity_id=? ORDER BY f.decided_at DESC""",
        (identity_id,),
    ).fetchall()
    return [
        {
            "decision": r["decision"],
            "song": f"{r['name']} — {', '.join(a['name'] for a in json.loads(r['artists_json']))}",
            "was": r["draft_state"],
            "score": r["score"],
            "decided_at": r["decided_at"],
            "uri": r["uri"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Learning and metrics
# ---------------------------------------------------------------------------


def suggest_avoid_tags(
    db: Database,
    identity_id: int,
    descriptors: dict[str, Descriptors],
    *,
    min_rejects: int = 3,
    reject_support: float = 0.5,
    example_support: float = 0.15,
) -> list[dict[str, Any]]:
    """Tags on at least half your rejected songs but rare among the playlist's examples."""
    rec_of = {
        r[0]: r[1] for r in db.conn.execute("SELECT uri, recording_id FROM recording_members")
    }

    def vecs(role: str) -> list[dict[str, float]]:
        uris = [
            r[0]
            for r in db.conn.execute(
                "SELECT uri FROM identity_examples WHERE identity_id=? AND role=?",
                (identity_id, role),
            )
        ]
        out = []
        for u in uris:
            d = descriptors.get(rec_of.get(u, ""))
            if d and d.combined:
                out.append(d.combined)
        return out

    rejected, examples = vecs("exclude"), vecs("reference") + vecs("essential")
    if len(rejected) < min_rejects:
        return []

    def support(vs: list[dict[str, float]]) -> Counter:
        c: Counter = Counter()
        for v in vs:
            for t, w in v.items():
                if w >= 0.3:
                    c[t] += 1
        return c

    rc, ec = support(rejected), support(examples)
    ident = get_identity(db, identity_id)
    already = set(ident.rules.get("avoid_tags") or []) | set(ident.rules.get("tags") or {})
    out = []
    for t, n in rc.most_common():
        rs = n / len(rejected)
        es = ec[t] / len(examples) if examples else 0.0
        if rs >= reject_support and es <= example_support and t not in already:
            out.append({"tag": t, "in_rejected": round(rs, 2), "in_examples": round(es, 2)})
    return out[:5]


@dataclass
class PlaylistStats:
    name: str
    kept: int
    rejected: int
    essential: int
    uncertain: int
    suggested_kept: int  # you kept a song the draft had included
    suggested_rejected: int  # you rejected a song the draft had included
    review_kept: int
    review_rejected: int
    manual_adds: int  # kept/essential songs the draft hadn't suggested

    @property
    def acceptance_rate(self) -> float | None:
        n = self.suggested_kept + self.suggested_rejected
        return self.suggested_kept / n if n else None


def stats(db: Database) -> list[PlaylistStats]:
    out = []
    for ident in list_identities(db):
        rows = db.conn.execute(
            "SELECT decision, draft_state FROM feedback WHERE identity_id=?", (ident.identity_id,)
        ).fetchall()
        c = Counter((r["decision"], r["draft_state"]) for r in rows)
        dec = Counter(r["decision"] for r in rows)
        out.append(
            PlaylistStats(
                name=ident.name,
                kept=dec["keep"],
                rejected=dec["reject"],
                essential=dec["essential"],
                uncertain=dec["uncertain"],
                suggested_kept=c[("keep", "include")] + c[("essential", "include")],
                suggested_rejected=c[("reject", "include")],
                review_kept=c[("keep", "review")] + c[("essential", "review")],
                review_rejected=c[("reject", "review")],
                manual_adds=c[("keep", "none")] + c[("essential", "none")],
            )
        )
    return out


@dataclass
class Evaluation:
    name: str
    decided: int
    agreed: int
    kept_but_would_drop: list[str]
    rejected_but_would_add: list[str]

    @property
    def agreement(self) -> float | None:
        return self.agreed / self.decided if self.decided else None


def evaluate(db: Database, descriptors: dict[str, Descriptors] | None = None) -> list[Evaluation]:
    """Holdout check: re-score each decided song with its own decision hidden.

    A kept song "agrees" if the playlist would include it anyway; a rejected song agrees if
    the playlist would leave it out. Uncertain decisions aren't counted.
    """
    from playlist_curator.curator.scoring import load_library, score_playlist

    lib = load_library(db, descriptors)
    results = []
    for ident in list_identities(db):
        if not ident.auto_maintain:
            continue
        rows = db.conn.execute(
            "SELECT uri, decision FROM feedback WHERE identity_id=? AND decision IN "
            "('keep','reject')",
            (ident.identity_id,),
        ).fetchall()
        if not rows:
            continue
        ev = Evaluation(ident.name, 0, 0, [], [])
        for r in rows:
            rid = lib.rec_of_uri.get(r["uri"])
            if not rid:
                continue
            draft = score_playlist(db, ident, lib, hide={r["uri"]})
            included = rid in {s.recording_id for s in draft.include}
            ev.decided += 1
            agree = included if r["decision"] == "keep" else not included
            if agree:
                ev.agreed += 1
            elif r["decision"] == "keep":
                ev.kept_but_would_drop.append(lib.titles[rid])
            else:
                ev.rejected_but_would_add.append(lib.titles[rid])
        results.append(ev)
    return results
