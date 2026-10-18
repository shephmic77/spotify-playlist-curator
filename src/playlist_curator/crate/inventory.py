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


def track_table(db: Database) -> list[dict[str, Any]]:
    """One row per unique track with its sources. Used by the Library view."""
    rows = db.conn.execute(
        """
        SELECT t.uri, t.name, t.artists_json, t.album_name, t.release_date, t.duration_ms,
               t.isrc, t.is_local,
               MAX(se.kind='liked') AS liked
        FROM tracks t
        JOIN source_entries se ON se.track_uri = t.uri
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
