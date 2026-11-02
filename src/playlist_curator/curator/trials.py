"""Trial songs: songs you asked to try in one playlist before they join your library.

Owner's decision 2026-11-01. The normal rule is that playlists only use songs already in your
crate (Liked Songs + your playlists). A trial is the one exception, and only when you ask:

- ``add``: you (or Claude, from what you said you like) name exact songs. Each is looked up
  with one Spotify search for that title and artist; nothing is recommended by Spotify and
  nothing is added that you didn't name. A found song is **pending**: it goes into that one
  app-made playlist only, and is *not* in your crate.
- **like** (``curator trial like``, or heart it in Spotify): it joins your crate and is pinned
  in that playlist (essential).
- **dislike** (``curator trial dislike``, or remove it from the playlist in Spotify): it leaves
  the playlist, never joins the crate, and is never added again. Only its title and artist are
  kept, as a "don't suggest this again" record.

Matching stays rule-based. This module never sends anything to an AI. It imports models (pure
parsers), not the Spotify client: the caller passes search results in.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from playlist_curator.curator import feedback as fb
from playlist_curator.curator.identities import Identity
from playlist_curator.models import SourceEntry, SourceKind, Track, parse_track
from playlist_curator.storage.database import Database, utcnow

TRIAL_SOURCE_ID = "trials"
Search = Callable[[str], list[dict[str, Any]]]


@dataclass
class AddResult:
    query: str
    status: str  # added | already_trial | in_library | disliked_before | not_found
    label: str = ""


def split_query(query: str) -> tuple[str, str | None]:
    """'El Paso - Marty Robbins' / 'El Paso — Marty Robbins' / 'El Paso by Marty Robbins'."""
    for sep in (" — ", " – ", " - ", " by "):
        if sep in query:
            title, artist = query.split(sep, 1)
            return title.strip(), artist.strip() or None
    return query.strip(), None


def _base(title: str) -> str:
    from playlist_curator.crate.deduplication import parse_title

    return parse_title(title).base


def _artist(name: str) -> str:
    from playlist_curator.crate.deduplication import normalize_artist

    return normalize_artist(name)


def _live_or_alt(title: str) -> bool:
    return bool(re.search(r"\b(live|demo|karaoke|instrumental|cover|remix)\b", title, re.I))


def best_match(query: str, results: list[dict[str, Any]]) -> Track | None:
    """The search result that is really the song you named: same title (ignoring "Remastered"
    and the like) and, if you gave an artist, that artist among its credits. Studio versions
    before live/demo/remix ones, unless you asked for one. None if nothing matches: a near
    miss is never substituted."""
    title, artist = split_query(query)
    want_title, want_artist = _base(title), _artist(artist) if artist else None
    asked_alt = _live_or_alt(title)
    hits = []
    for raw in results:
        t = parse_track(raw).track
        if t is None or t.is_local:
            continue
        if _base(t.name) != want_title:
            continue
        if want_artist and not any(
            want_artist == _artist(a.name) or want_artist in _artist(a.name) for a in t.artists
        ):
            continue
        hits.append(t)
    if not hits:
        return None
    if not asked_alt:
        studio = [t for t in hits if not _live_or_alt(t.name)]
        hits = studio or hits
    return hits[0]


def search_query(query: str) -> str:
    title, artist = split_query(query)
    return f'track:"{title}" artist:"{artist}"' if artist else f'track:"{title}"'


def _in_crate(db: Database, t: Track) -> str | None:
    """Label of a song already in your library that's the same recording (same ISRC, or same
    title and lead artist), else None."""
    rows = db.conn.execute(
        """SELECT DISTINCT t.uri, t.name, t.artists_json, t.isrc FROM tracks t
           JOIN source_entries s ON s.track_uri = t.uri"""
    ).fetchall()
    lead = _artist(t.artists[0].name) if t.artists else ""
    for r in rows:
        names = [a["name"] for a in json.loads(r["artists_json"])]
        if (t.isrc and r["isrc"] == t.isrc) or (
            _base(r["name"]) == _base(t.name) and names and _artist(names[0]) == lead
        ):
            return f"{r['name']} — {', '.join(names)}"
    return None


def _label(title: str, artists: list[str]) -> str:
    return f"{title} — {', '.join(artists)}"


def add(db: Database, ident: Identity, queries: list[str], search: Search) -> list[AddResult]:
    """Look up each named song and add it to this playlist as a pending trial."""
    out = []
    for q in queries:
        q = q.strip()
        if not q:
            continue
        t = best_match(q, search(search_query(q)))
        if t is None:
            out.append(AddResult(q, "not_found"))
            continue
        label = _label(t.name, [a.name for a in t.artists])
        prev = db.conn.execute(
            "SELECT status FROM trial_songs WHERE identity_id=? AND (uri=? OR (title=? AND"
            " artists_json=?))",
            (ident.identity_id, t.uri, t.name, json.dumps([a.name for a in t.artists])),
        ).fetchone()
        if prev and prev["status"] == "disliked":
            out.append(AddResult(q, "disliked_before", label))
            continue
        if prev:
            out.append(AddResult(q, "already_trial", label))
            continue
        have = _in_crate(db, t)
        if have:
            out.append(AddResult(q, "in_library", have))
            continue
        db.conn.execute(
            "INSERT INTO trial_songs VALUES (?,?,?,?,?,?, 'pending', ?, NULL)",
            (
                ident.identity_id,
                t.uri,
                q,
                t.name,
                json.dumps([a.name for a in t.artists]),
                t.model_dump_json(),
                utcnow(),
            ),
        )
        out.append(AddResult(q, "added", label))
    return out


def rows(db: Database, identity_id: int | None = None, status: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM trial_songs WHERE 1=1", []
    if identity_id is not None:
        sql += " AND identity_id=?"
        args.append(identity_id)
    if status:
        sql += " AND status=?"
        args.append(status)
    return [dict(r) for r in db.conn.execute(sql + " ORDER BY added_at, title", args)]


def pending_uris(db: Database, identity_id: int) -> list[str]:
    return [r["uri"] for r in rows(db, identity_id, "pending")]


def names(db: Database) -> dict[str, tuple[str, list[str]]]:
    """uri -> (title, artists) for every trial, so trial songs can be labelled and sorted."""
    return {
        r["uri"]: (r["title"], json.loads(r["artists_json"]))
        for r in db.conn.execute("SELECT uri, title, artists_json FROM trial_songs")
    }


def find(db: Database, identity_id: int, query: str) -> dict:
    title, artist = split_query(query)
    want, want_artist = _base(title), _artist(artist) if artist else None
    hits = [
        r
        for r in rows(db, identity_id)
        if _base(r["title"]) == want
        and (
            not want_artist or any(want_artist in _artist(a) for a in json.loads(r["artists_json"]))
        )
    ]
    if not hits:
        raise LookupError(
            f"No trial song like {query!r} in this playlist (see: curator trial list)"
        )
    if len(hits) > 1:
        opts = "; ".join(_label(h["title"], json.loads(h["artists_json"])) for h in hits)
        raise LookupError(f"{query!r} matches several trial songs: {opts}. Add the artist.")
    return hits[0]


def _write_crate_entries(db: Database) -> None:
    """Liked trials are a source of the crate, like Liked Songs or a playlist."""
    liked = rows(db, status="liked")
    tracks = [Track.model_validate_json(r["track_json"]) for r in liked if r["track_json"]]
    with db.transaction():
        if tracks:
            db.upsert_tracks(tracks, seen_at=utcnow())
        db.replace_source_entries(
            SourceKind.TRIAL,
            TRIAL_SOURCE_ID,
            [
                SourceEntry(
                    track_uri=t.uri, kind=SourceKind.TRIAL, source_id=TRIAL_SOURCE_ID, position=i
                )
                for i, t in enumerate(tracks)
            ],
        )


def like(db: Database, ident: Identity, row: dict) -> str:
    """Into the crate, pinned in this playlist."""
    from playlist_curator.crate.deduplication import rebuild

    db.conn.execute(
        "UPDATE trial_songs SET status='liked', decided_at=? WHERE identity_id=? AND uri=?",
        (utcnow(), ident.identity_id, row["uri"]),
    )
    _write_crate_entries(db)
    rebuild(db)
    primary = db.conn.execute(
        """SELECT r.primary_uri FROM recording_members m JOIN recordings r
           ON r.recording_id = m.recording_id WHERE m.uri=?""",
        (row["uri"],),
    ).fetchone()
    fb.decide_uri(db, ident, primary[0] if primary else row["uri"], "essential")
    return _label(row["title"], json.loads(row["artists_json"]))


def dislike(db: Database, ident: Identity, row: dict) -> str:
    """Out of the playlist at the next sync; never in the crate; never added again."""
    db.conn.execute(
        "UPDATE trial_songs SET status='disliked', decided_at=?, track_json=NULL"
        " WHERE identity_id=? AND uri=?",
        (utcnow(), ident.identity_id, row["uri"]),
    )
    if row["status"] == "liked":  # changed your mind: take it back out of the crate
        _write_crate_entries(db)
        db.delete_orphan_tracks()
        from playlist_curator.crate.deduplication import rebuild

        rebuild(db)
    return _label(row["title"], json.loads(row["artists_json"]))


def reconcile(db: Database, idents: dict[int, Identity]) -> list[str]:
    """Pending trials you hearted in Spotify (now in Liked Songs after an import) count as
    liked. Returns their labels."""
    in_crate = {
        r[0]
        for r in db.conn.execute(
            "SELECT track_uri FROM source_entries WHERE kind != ?", (SourceKind.TRIAL.value,)
        )
    }
    done = []
    for r in rows(db, status="pending"):
        ident = idents.get(r["identity_id"])
        if ident is not None and r["uri"] in in_crate:
            done.append(like(db, ident, r))
    return done
