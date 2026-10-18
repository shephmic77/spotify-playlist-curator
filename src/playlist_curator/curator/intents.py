"""Your descriptions of what each old playlist was for.

The new collection is built from scratch, so these are *evidence*, not instructions to keep
anything: a description tells the proposal engine what kind of concept a group of songs
represents ("workout", "indie chill", "songs for the car with my parents").

Kinds decide how a description is used:
- sound, mood, situation, era  → can seed a concept for the new collection
- handpicked                    → may come back as an optional hand-picked playlist
                                  (the app never adds songs to those automatically)
- source, catch_all, mixed, unknown → not a concept; the songs simply stay in the crate
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playlist_curator.storage.database import Database, utcnow

CONCEPT_KINDS = ("sound", "mood", "situation", "era")
HANDPICKED_KINDS = ("handpicked",)
OTHER_KINDS = ("source", "catch_all", "mixed", "unknown")
KINDS = CONCEPT_KINDS + HANDPICKED_KINDS + OTHER_KINDS


class PlaylistNotFound(LookupError):
    pass


class AmbiguousPlaylist(LookupError):
    def __init__(self, ref: str, candidates: list[sqlite3.Row]) -> None:
        self.candidates = candidates
        listing = "; ".join(
            f"{c['spotify_id']} ({c['name']}, {c['stored_entries']} songs)" for c in candidates
        )
        super().__init__(f"{ref!r} matches several playlists: {listing}. Use the id instead.")


@dataclass
class Intent:
    playlist_id: str
    playlist_name: str
    songs: int
    title: str
    intent: str
    kind: str


def _playlists(db: Database) -> list[sqlite3.Row]:
    return db.list_playlists()


def resolve_playlist(db: Database, ref: str) -> sqlite3.Row:
    """Find an old playlist by id, id prefix, or name (names can repeat, e.g. two "H")."""
    rows = _playlists(db)
    by_id = [r for r in rows if r["spotify_id"] == ref]
    if by_id:
        return by_id[0]
    exact = [r for r in rows if r["name"] == ref]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousPlaylist(ref, exact)
    if len(ref) >= 4:
        prefix = [r for r in rows if r["spotify_id"].startswith(ref)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise AmbiguousPlaylist(ref, prefix)
    folded = [r for r in rows if r["name"].casefold() == ref.casefold()]
    if len(folded) == 1:
        return folded[0]
    if len(folded) > 1:
        raise AmbiguousPlaylist(ref, folded)
    raise PlaylistNotFound(f"No imported playlist called {ref!r}. See: curator playlists")


def set_intent(db: Database, playlist_ref: str, intent: str, kind: str, title: str = "") -> Intent:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    p = resolve_playlist(db, playlist_ref)
    title = (title or intent).strip()[:60]
    db.conn.execute(
        "INSERT OR REPLACE INTO playlist_intents VALUES (?,?,?,?,?)",
        (p["spotify_id"], title, intent.strip(), kind, utcnow()),
    )
    return Intent(p["spotify_id"], p["name"], p["stored_entries"], title, intent.strip(), kind)


def delete_intent(db: Database, playlist_ref: str) -> bool:
    p = resolve_playlist(db, playlist_ref)
    cur = db.conn.execute("DELETE FROM playlist_intents WHERE playlist_id=?", (p["spotify_id"],))
    return cur.rowcount > 0


def list_intents(db: Database) -> list[Intent]:
    rows = db.conn.execute(
        """SELECT i.*, p.name,
                  (SELECT COUNT(*) FROM source_entries se
                    WHERE se.kind='playlist' AND se.source_id=i.playlist_id) AS songs
           FROM playlist_intents i JOIN playlists p ON p.spotify_id = i.playlist_id
           ORDER BY p.name COLLATE NOCASE"""
    ).fetchall()
    return [
        Intent(r["playlist_id"], r["name"], r["songs"], r["title"], r["intent"], r["kind"])
        for r in rows
    ]


def import_intents(db: Database, path: Path) -> tuple[list[Intent], list[str]]:
    """Load descriptions from a JSON list of {playlist, title, intent, kind}.

    Entries whose playlist name is ambiguous are reported, not guessed.
    """
    entries: list[dict[str, Any]] = json.loads(Path(path).read_text(encoding="utf-8"))
    applied: list[Intent] = []
    problems: list[str] = []
    for e in entries:
        ref = e.get("playlist_id") or e.get("playlist")
        if not ref:
            problems.append(f"entry without a playlist: {e}")
            continue
        try:
            applied.append(
                set_intent(
                    db, ref, e.get("intent", ""), e.get("kind", "unknown"), e.get("title", "")
                )
            )
        except (PlaylistNotFound, AmbiguousPlaylist, ValueError) as exc:
            problems.append(str(exc))
    return applied, problems
