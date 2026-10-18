"""Playlist identities: the definitions of the playlists in your new collection.

An identity is created when you accept a proposal. It holds:
- name, kind, and a plain-language concept
- rules: tags it's about (weights 0–1), tags to avoid, an optional preferred year range
- example songs (seed references from the old playlists or tag cluster; you can add more,
  exclude songs, or mark songs essential in the feedback phase)
- auto_maintain: whether the app may add songs on its own (off for hand-picked kinds)

Phase 4 scores songs against these; nothing here touches Spotify.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from playlist_curator.curator.intents import KINDS
from playlist_curator.storage.database import Database, utcnow

STATUSES = ("approved", "rejected", "retired")


@dataclass
class Identity:
    identity_id: int
    name: str
    kind: str
    concept: str
    status: str
    auto_maintain: bool
    rules: dict[str, Any]
    origin: dict[str, Any]
    examples: dict[str, int] = field(default_factory=dict)  # role -> count


def _row(db: Database, r: Any) -> Identity:
    counts = {
        x[0]: x[1]
        for x in db.conn.execute(
            "SELECT role, COUNT(*) FROM identity_examples WHERE identity_id=? GROUP BY role",
            (r["identity_id"],),
        )
    }
    return Identity(
        r["identity_id"],
        r["name"],
        r["kind"],
        r["concept"],
        r["status"],
        bool(r["auto_maintain"]),
        json.loads(r["rules_json"]),
        json.loads(r["origin_json"]),
        counts,
    )


def list_identities(db: Database, *, include_inactive: bool = False) -> list[Identity]:
    sql = "SELECT * FROM identities"
    if not include_inactive:
        sql += " WHERE status='approved'"
    sql += " ORDER BY name COLLATE NOCASE"
    return [_row(db, r) for r in db.conn.execute(sql).fetchall()]


def get_identity(db: Database, ref: str | int) -> Identity:
    rows = db.conn.execute("SELECT * FROM identities").fetchall()
    if str(ref).isdigit():
        hit = [r for r in rows if r["identity_id"] == int(ref)]
        if hit:
            return _row(db, hit[0])
    hit = [r for r in rows if r["name"].casefold() == str(ref).casefold()]
    if len(hit) == 1:
        return _row(db, hit[0])
    if len(hit) > 1:
        ids = ", ".join(str(r["identity_id"]) for r in hit)
        raise LookupError(f"Several playlists are called {ref!r} (ids {ids}); use the id.")
    raise LookupError(f"No playlist identity {ref!r}. See: curator identities")


def edit_identity(
    db: Database,
    ref: str | int,
    *,
    name: str | None = None,
    concept: str | None = None,
    kind: str | None = None,
    add_tags: dict[str, float] | None = None,
    remove_tags: list[str] | None = None,
    avoid_tags: list[str] | None = None,
    unavoid_tags: list[str] | None = None,
    require_tags: list[str] | None = None,
    unrequire_tags: list[str] | None = None,
    years: list[int] | str | None = "keep",
    auto_maintain: bool | None = None,
) -> Identity:
    ident = get_identity(db, ref)
    rules = dict(ident.rules)

    # Removals first, then additions, so "replace the list" edits (as the UI does) work.
    def norm(xs: list[str] | None) -> list[str]:
        return [x.strip().lower() for x in xs or [] if x.strip()]

    tags = dict(rules.get("tags") or {})
    for t in norm(remove_tags):
        tags.pop(t, None)
    for t, w in (add_tags or {}).items():
        if not 0 < w <= 1:
            raise ValueError(f"tag weight for {t!r} must be in (0, 1]")
        tags[t.strip().lower()] = w
    avoid = [t for t in rules.get("avoid_tags") or [] if t not in set(norm(unavoid_tags))]
    avoid += [t for t in norm(avoid_tags) if t not in avoid]
    # A song must carry at least one required tag to qualify (e.g. "Classical" = classical only)
    required = [t for t in rules.get("require_tags") or [] if t not in set(norm(unrequire_tags))]
    required += [t for t in norm(require_tags) if t not in required]
    rules["tags"], rules["avoid_tags"], rules["require_tags"] = tags, avoid, required
    if years != "keep":
        if years is not None and (len(years) != 2 or years[0] > years[1]):
            raise ValueError("years must be FROM-TO, e.g. 1980-1989")
        rules["years"] = years
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    db.conn.execute(
        """UPDATE identities SET name=?, concept=?, kind=?, rules_json=?, auto_maintain=?,
               updated_at=? WHERE identity_id=?""",
        (
            name if name is not None else ident.name,
            concept if concept is not None else ident.concept,
            kind if kind is not None else ident.kind,
            json.dumps(rules),
            int(auto_maintain if auto_maintain is not None else ident.auto_maintain),
            utcnow(),
            ident.identity_id,
        ),
    )
    return get_identity(db, ident.identity_id)


def create_identity(
    db: Database,
    name: str,
    *,
    kind: str = "sound",
    concept: str = "",
    like: str | int | None = None,
    auto_maintain: bool = True,
) -> Identity:
    """Create a playlist without a proposal, optionally copying rules and examples from another."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    rules: dict[str, Any] = {
        "tags": {},
        "avoid_tags": [],
        "require_tags": [],
        "years": None,
        "years_mode": "prefer",
    }
    origin: dict[str, Any] = {"created": "manual"}
    source = get_identity(db, like) if like is not None else None
    if source is not None:
        rules = json.loads(json.dumps(source.rules))
        origin = {"created": "copy", "copied_from": source.identity_id}
    now = utcnow()
    with db.transaction():
        cur = db.conn.execute(
            """INSERT INTO identities (name, kind, concept, status, auto_maintain, rules_json,
                   origin_json, created_at, updated_at) VALUES (?,?,?,'approved',?,?,?,?,?)""",
            (
                name,
                kind,
                concept,
                int(auto_maintain),
                json.dumps(rules),
                json.dumps(origin),
                now,
                now,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        if source is not None:
            db.conn.execute(
                """INSERT INTO identity_examples (identity_id, uri, role, source, added_at)
                   SELECT ?, uri, role, source, ? FROM identity_examples WHERE identity_id=?""",
                (new_id, now, source.identity_id),
            )
    return get_identity(db, new_id)


def set_status(db: Database, ref: str | int, status: str) -> Identity:
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    ident = get_identity(db, ref)
    db.conn.execute(
        "UPDATE identities SET status=?, updated_at=? WHERE identity_id=?",
        (status, utcnow(), ident.identity_id),
    )
    return get_identity(db, ident.identity_id)


def example_songs(db: Database, identity_id: int, limit: int = 15) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        """SELECT e.role, e.source, t.name, t.artists_json FROM identity_examples e
           JOIN tracks t ON t.uri = e.uri WHERE e.identity_id=?
           ORDER BY e.role, t.name COLLATE NOCASE LIMIT ?""",
        (identity_id, limit),
    ).fetchall()
    return [
        {
            "role": r["role"],
            "source": r["source"],
            "title": r["name"],
            "artists": ", ".join(a["name"] for a in json.loads(r["artists_json"])),
        }
        for r in rows
    ]


def parse_years(text: str) -> list[int] | None:
    if text.strip().lower() in ("none", "any", ""):
        return None
    lo, _, hi = text.partition("-")
    return [int(lo), int(hi or lo)]


def parse_tag_weights(items: list[str]) -> dict[str, float]:
    """ "synthwave" -> 1.0, "80s=0.6" -> 0.6"""
    out = {}
    for item in items:
        tag, _, w = item.partition("=")
        out[tag.strip().lower()] = float(w) if w else 1.0
    return out
