"""Read-only summaries of the imported library, shared by the CLI and the UI."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from playlist_curator.storage.database import Database


@dataclass
class InventorySummary:
    unique_tracks: int = 0
    catalog_tracks: int = 0
    local_tracks: int = 0
    in_liked: int = 0
    in_playlists: int = 0
    in_both: int = 0
    liked_only: int = 0
    playlists_only: int = 0
    liked_entries: int = 0
    playlist_entries: int = 0
    repeated_within_playlist: int = 0
    isrc_groups_with_multiple_uris: int = 0
    tracks_in_isrc_groups: int = 0
    catalog_missing_isrc: int = 0
    playlists_total: int = 0
    playlists_owned: int = 0
    playlists_fetched: int = 0
    playlists_contents_unavailable: int = 0
    playlists_error: int = 0
    local_file_playlists: list[str] = field(default_factory=list)
    # Recording level (Phase 2): duplicates merged
    unique_recordings: int = 0
    merged_groups: int = 0
    duplicate_uris_merged: int = 0
    rec_in_liked: int = 0
    rec_in_playlists: int = 0
    rec_in_both: int = 0
    rec_liked_only: int = 0
    rec_playlists_only: int = 0
    local_matched: int = 0
    local_unmatched: int = 0
    pending_title_suggestions: int = 0
    pending_local_suggestions: int = 0
    missing_release_date: int = 0
    missing_artist: int = 0
    missing_duration: int = 0
    last_run: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize(db: Database) -> InventorySummary:
    c = db.conn
    s = InventorySummary()

    row = c.execute(
        """
        WITH per_track AS (
            SELECT t.uri, t.is_local,
                   MAX(se.kind = 'liked')    AS liked,
                   MAX(se.kind = 'playlist') AS listed
            FROM tracks t JOIN source_entries se ON se.track_uri = t.uri
            GROUP BY t.uri
        )
        SELECT COUNT(*)                              AS unique_tracks,
               COALESCE(SUM(is_local = 0), 0)        AS catalog_tracks,
               COALESCE(SUM(is_local = 1), 0)        AS local_tracks,
               COALESCE(SUM(liked), 0)               AS in_liked,
               COALESCE(SUM(listed), 0)              AS in_playlists,
               COALESCE(SUM(liked AND listed), 0)    AS in_both,
               COALESCE(SUM(liked AND NOT listed), 0) AS liked_only,
               COALESCE(SUM(listed AND NOT liked), 0) AS playlists_only
        FROM per_track
        """
    ).fetchone()
    for key in row.keys():  # noqa: SIM118 - sqlite3.Row is not a dict
        setattr(s, key, int(row[key]))

    s.liked_entries = c.execute(
        "SELECT COUNT(*) FROM source_entries WHERE kind='liked'"
    ).fetchone()[0]
    s.playlist_entries = c.execute(
        "SELECT COUNT(*) FROM source_entries WHERE kind='playlist'"
    ).fetchone()[0]
    s.repeated_within_playlist = c.execute(
        """SELECT COALESCE(SUM(n - 1), 0) FROM (
               SELECT COUNT(*) AS n FROM source_entries WHERE kind='playlist'
               GROUP BY source_id, track_uri HAVING n > 1)"""
    ).fetchone()[0]

    # Same ISRC under different URIs: likely the same recording released more than once
    # (single + album, regional relinks). Reported only; merging is Phase 2's job.
    groups = c.execute(
        """SELECT COUNT(*), COALESCE(SUM(n), 0) FROM (
               SELECT isrc, COUNT(*) AS n FROM tracks
               WHERE isrc IS NOT NULL AND is_local = 0
               GROUP BY isrc HAVING n > 1)"""
    ).fetchone()
    s.isrc_groups_with_multiple_uris, s.tracks_in_isrc_groups = int(groups[0]), int(groups[1])
    s.catalog_missing_isrc = c.execute(
        "SELECT COUNT(*) FROM tracks WHERE is_local = 0 AND isrc IS NULL"
    ).fetchone()[0]

    p = c.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(owned_by_user), 0) AS owned,
                  COALESCE(SUM(contents_status='fetched'), 0) AS fetched,
                  COALESCE(SUM(contents_status='unavailable'), 0) AS unavailable,
                  COALESCE(SUM(contents_status='error'), 0) AS error
           FROM playlists"""
    ).fetchone()
    s.playlists_total = int(p["total"])
    s.playlists_owned = int(p["owned"])
    s.playlists_fetched = int(p["fetched"])
    s.playlists_contents_unavailable = int(p["unavailable"])
    s.playlists_error = int(p["error"])

    s.local_file_playlists = [
        r[0]
        for r in c.execute(
            """SELECT p.name FROM playlists p
               JOIN source_entries se ON se.kind='playlist' AND se.source_id=p.spotify_id
               JOIN tracks t ON t.uri = se.track_uri
               WHERE t.is_local = 1 GROUP BY p.spotify_id ORDER BY p.name"""
        )
    ]

    _recording_stats(db, s)

    runs = db.last_runs(1)
    if runs:
        r = runs[0]
        s.last_run = {
            "id": r["id"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "counts": json.loads(r["counts_json"]) if r["counts_json"] else None,
            "errors": json.loads(r["errors_json"]) if r["errors_json"] else [],
        }
    return s


def _recording_stats(db: Database, s: InventorySummary) -> None:
    c = db.conn
    row = c.execute(
        """
        WITH per_rec AS (
            SELECT rm.recording_id,
                   MAX(se.kind = 'liked')    AS liked,
                   MAX(se.kind = 'playlist') AS listed
            FROM recording_members rm JOIN source_entries se ON se.track_uri = rm.uri
            GROUP BY rm.recording_id
        )
        SELECT COUNT(*)                               AS unique_recordings,
               COALESCE(SUM(liked), 0)                AS rec_in_liked,
               COALESCE(SUM(listed), 0)               AS rec_in_playlists,
               COALESCE(SUM(liked AND listed), 0)     AS rec_in_both,
               COALESCE(SUM(liked AND NOT listed), 0) AS rec_liked_only,
               COALESCE(SUM(listed AND NOT liked), 0) AS rec_playlists_only
        FROM per_rec
        """
    ).fetchone()
    for key in row.keys():  # noqa: SIM118 - sqlite3.Row is not a dict
        setattr(s, key, int(row[key]))
    merged = c.execute(
        "SELECT COUNT(*), COALESCE(SUM(n_members), 0) FROM recordings WHERE n_members > 1"
    ).fetchone()
    s.merged_groups = int(merged[0])
    s.duplicate_uris_merged = int(merged[1]) - int(merged[0])
    loc = c.execute(
        """SELECT COALESCE(SUM(r.has_catalog = 1), 0), COALESCE(SUM(r.has_catalog = 0), 0)
           FROM tracks t JOIN recording_members rm ON rm.uri = t.uri
           JOIN recordings r ON r.recording_id = rm.recording_id
           WHERE t.is_local = 1"""
    ).fetchone()
    s.local_matched, s.local_unmatched = int(loc[0]), int(loc[1])
    for kind, attr in (
        ("title_match", "pending_title_suggestions"),
        ("local_match", "pending_local_suggestions"),
    ):
        n = c.execute("SELECT COUNT(*) FROM dedup_suggestions WHERE kind=?", (kind,)).fetchone()
        setattr(s, attr, int(n[0]))
    m = c.execute(
        """SELECT COALESCE(SUM(is_local = 0 AND (release_date IS NULL OR release_date = '')), 0),
                  COALESCE(SUM(artists_json = '[]'), 0),
                  COALESCE(SUM(duration_ms IS NULL), 0)
           FROM tracks"""
    ).fetchone()
    s.missing_release_date, s.missing_artist, s.missing_duration = (int(x) for x in m)


def track_table(db: Database) -> list[dict[str, Any]]:
    """One row per unique track with its sources. Used by the Library view."""
    rows = db.conn.execute(
        """
        SELECT t.uri, t.name, t.artists_json, t.album_name, t.release_date, t.duration_ms,
               t.isrc, t.is_local, rm.recording_id, r.n_members,
               MAX(se.kind='liked') AS liked
        FROM tracks t
        JOIN source_entries se ON se.track_uri = t.uri
        LEFT JOIN recording_members rm ON rm.uri = t.uri
        LEFT JOIN recordings r ON r.recording_id = rm.recording_id
        GROUP BY t.uri
        ORDER BY t.name COLLATE NOCASE
        """
    ).fetchall()
    # Playlist names can contain commas, so build the lists in Python rather than GROUP_CONCAT.
    membership: dict[str, list[str]] = {}
    for uri, name in db.conn.execute(
        """SELECT DISTINCT se.track_uri, p.name FROM source_entries se
           JOIN playlists p ON p.spotify_id = se.source_id
           WHERE se.kind = 'playlist' ORDER BY p.name COLLATE NOCASE"""
    ):
        membership.setdefault(uri, []).append(name)
    out = []
    for r in rows:
        names = membership.get(r["uri"], [])
        artists = ", ".join(a["name"] for a in json.loads(r["artists_json"]))
        out.append(
            {
                "name": r["name"],
                "artists": artists,
                "album": r["album_name"],
                "release_date": r["release_date"],
                "duration_s": round(r["duration_ms"] / 1000) if r["duration_ms"] else None,
                "liked": bool(r["liked"]),
                "n_playlists": len(names),
                "playlists": names,
                "local_file": bool(r["is_local"]),
                "isrc": r["isrc"],
                "copies_of_recording": r["n_members"] or 1,
                "recording_id": r["recording_id"],
                "uri": r["uri"],
            }
        )
    return out


def playlist_table(db: Database) -> list[dict[str, Any]]:
    return [
        {
            "name": r["name"],
            "owner": r["owner_name"] or r["owner_id"],
            "owned": bool(r["owned_by_user"]),
            "collaborative": bool(r["collaborative"]),
            "public": None if r["public"] is None else bool(r["public"]),
            "reported_items": r["total_items"],
            "imported_entries": r["stored_entries"],
            "contents": r["contents_status"],
            "note": r["contents_note"] or "",
            "last_fetched": r["last_fetched_at"],
            "id": r["spotify_id"],
        }
        for r in db.list_playlists()
    ]


def _track_info(db: Database, uri: str, source_counts: dict[str, int]) -> dict[str, Any]:
    t = db.get_track(uri)
    if t is None:
        return {"uri": uri, "name": "(no longer in crate)"}
    return {
        "uri": uri,
        "name": t.name,
        "artists": t.artist_names,
        "album": t.album_name,
        "album_type": t.album_type,
        "release_date": t.release_date,
        "duration_s": round(t.duration_ms / 1000) if t.duration_ms else None,
        "isrc": t.isrc,
        "local_file": t.is_local,
        "times_in_sources": source_counts.get(uri, 0),
    }


def _source_counts(db: Database) -> dict[str, int]:
    return {
        r[0]: r[1]
        for r in db.conn.execute(
            "SELECT track_uri, COUNT(*) FROM source_entries GROUP BY track_uri"
        )
    }


def duplicate_groups(db: Database) -> list[dict[str, Any]]:
    """Recordings that have more than one URI, with every member and why it was grouped."""
    counts = _source_counts(db)
    groups = []
    for r in db.conn.execute(
        "SELECT * FROM recordings WHERE n_members > 1 ORDER BY title COLLATE NOCASE"
    ).fetchall():
        members = []
        for m in db.conn.execute(
            "SELECT uri, evidence FROM recording_members WHERE recording_id=? ORDER BY uri",
            (r["recording_id"],),
        ):
            info = _track_info(db, m["uri"], counts)
            info["evidence"] = m["evidence"]
            info["primary"] = m["uri"] == r["primary_uri"]
            members.append(info)
        members.sort(key=lambda x: not x["primary"])
        groups.append(
            {
                "recording_id": r["recording_id"],
                "title": r["title"],
                "artists": ", ".join(json.loads(r["artists_json"])),
                "members": members,
            }
        )
    return groups


def pending_suggestions(db: Database, kind: str | None = None) -> list[dict[str, Any]]:
    from playlist_curator.crate.deduplication import short_id

    counts = _source_counts(db)
    sql = "SELECT * FROM dedup_suggestions"
    params: tuple[str, ...] = ()
    if kind:
        sql += " WHERE kind=?"
        params = (kind,)
    rows = db.conn.execute(sql, params).fetchall()
    out = [
        {
            "id": short_id(r["pair_key"]),
            "pair_key": r["pair_key"],
            "kind": r["kind"],
            "strength": r["strength"],
            "reason": r["reason"],
            "a": _track_info(db, r["uri_a"], counts),
            "b": _track_info(db, r["uri_b"], counts),
        }
        for r in rows
    ]
    out.sort(key=lambda s: (s["kind"], s["strength"] != "strong", s["a"].get("name", "")))
    return out


def decisions_table(db: Database) -> list[dict[str, Any]]:
    from playlist_curator.crate.deduplication import short_id

    counts = _source_counts(db)
    return [
        {
            "id": short_id(r["pair_key"]),
            "pair_key": r["pair_key"],
            "decision": r["decision"],
            "decided_at": r["decided_at"],
            "a": _track_info(db, r["uri_a"], counts),
            "b": _track_info(db, r["uri_b"], counts),
        }
        for r in db.conn.execute("SELECT * FROM dedup_decisions ORDER BY decided_at DESC")
    ]


def local_files_table(db: Database) -> list[dict[str, Any]]:
    """Every local-file entry, where it lives, and what catalog recording it matches (if any)."""
    from playlist_curator.crate.deduplication import local_title_and_artist

    rows = []
    for t in (t for t in db.iter_tracks() if t.is_local):
        title, artist = local_title_and_artist(t)
        rec = db.conn.execute(
            """SELECT r.* FROM recording_members rm
               JOIN recordings r ON r.recording_id = rm.recording_id WHERE rm.uri=?""",
            (t.uri,),
        ).fetchone()
        matched = None
        if rec is not None and rec["has_catalog"]:
            matched = f"{rec['title']} — {', '.join(json.loads(rec['artists_json']))}"
        playlists = [
            r[0]
            for r in db.conn.execute(
                """SELECT DISTINCT p.name FROM source_entries se
                   JOIN playlists p ON p.spotify_id = se.source_id
                   WHERE se.kind='playlist' AND se.track_uri=?""",
                (t.uri,),
            )
        ]
        n_suggestions = db.conn.execute(
            "SELECT COUNT(*) FROM dedup_suggestions WHERE kind='local_match' AND uri_a=?",
            (t.uri,),
        ).fetchone()[0]
        rows.append(
            {
                "title": title or "(no title)",
                "artist": artist or "",
                "duration_s": round(t.duration_ms / 1000) if t.duration_ms else None,
                "in_playlists": playlists,
                "matched_recording": matched or "",
                "pending_suggestions": n_suggestions,
                "uri": t.uri,
            }
        )
    rows.sort(key=lambda r: (r["matched_recording"] != "", r["title"].lower()))
    return rows
