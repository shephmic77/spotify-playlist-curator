"""SQLite persistence for local application state.

Design notes
- One file, standard-library ``sqlite3``. No ORM: the schema is small and explicit SQL keeps
  migrations and data inspection simple.
- Schema changes go through ``MIGRATIONS`` (append-only). ``schema_version`` in ``meta``
  records how far a database has been migrated.
- Spotify's Developer Terms limit storing Spotify content to what the app needs and require
  deletion when you disconnect. ``purge`` wipes everything; the importer removes tracks that
  no longer appear in any source.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playlist_curator.models import (
    ArtistRef,
    ImportCounts,
    PlaylistSummary,
    SourceEntry,
    SourceKind,
    Track,
)

MIGRATIONS: list[str] = [
    # v1 - Phase 1: raw inventory
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE tracks (
        uri                    TEXT PRIMARY KEY,
        spotify_id             TEXT,
        name                   TEXT NOT NULL,
        artists_json           TEXT NOT NULL,
        album_name             TEXT,
        album_id               TEXT,
        album_type             TEXT,
        release_date           TEXT,
        release_date_precision TEXT,
        duration_ms            INTEGER,
        explicit               INTEGER,
        isrc                   TEXT,
        is_local               INTEGER NOT NULL DEFAULT 0,
        is_playable            INTEGER,
        disc_number            INTEGER,
        track_number           INTEGER,
        first_seen_at          TEXT NOT NULL,
        last_seen_at           TEXT NOT NULL
    );
    CREATE INDEX idx_tracks_isrc ON tracks(isrc);

    CREATE TABLE playlists (
        spotify_id          TEXT PRIMARY KEY,
        name                TEXT NOT NULL,
        description         TEXT,
        owner_id            TEXT,
        owner_name          TEXT,
        collaborative       INTEGER NOT NULL DEFAULT 0,
        public              INTEGER,
        snapshot_id         TEXT,
        total_items         INTEGER,
        owned_by_user       INTEGER NOT NULL DEFAULT 0,
        -- fetched | unavailable | error | pending
        contents_status     TEXT NOT NULL DEFAULT 'pending',
        contents_note       TEXT,
        fetched_snapshot_id TEXT,
        last_fetched_at     TEXT
    );

    CREATE TABLE source_entries (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        track_uri TEXT NOT NULL REFERENCES tracks(uri),
        kind      TEXT NOT NULL,
        source_id TEXT NOT NULL,
        position  INTEGER NOT NULL,
        added_at  TEXT,
        added_by  TEXT,
        UNIQUE (kind, source_id, position)
    );
    CREATE INDEX idx_source_entries_track ON source_entries(track_uri);
    CREATE INDEX idx_source_entries_source ON source_entries(kind, source_id);

    CREATE TABLE import_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        -- running | ok | partial | failed
        status      TEXT NOT NULL,
        counts_json TEXT,
        errors_json TEXT
    );
    """,
    # v2 - Phase 2: recording-level deduplication
    """
    -- Derived: rebuilt from tracks + dedup_decisions after every import. Safe to drop.
    CREATE TABLE recordings (
        recording_id  TEXT PRIMARY KEY,
        primary_uri   TEXT NOT NULL,
        title         TEXT NOT NULL,
        artists_json  TEXT NOT NULL,
        n_members     INTEGER NOT NULL,
        has_catalog   INTEGER NOT NULL,
        has_local     INTEGER NOT NULL
    );

    CREATE TABLE recording_members (
        uri          TEXT PRIMARY KEY REFERENCES tracks(uri) ON DELETE CASCADE,
        recording_id TEXT NOT NULL,
        -- self | isrc | accepted
        evidence     TEXT NOT NULL
    );
    CREATE INDEX idx_recording_members_rec ON recording_members(recording_id);

    -- Derived: pending suggestions for pairs the user has not decided yet.
    CREATE TABLE dedup_suggestions (
        pair_key TEXT PRIMARY KEY,
        uri_a    TEXT NOT NULL,
        uri_b    TEXT NOT NULL,
        -- title_match (catalog vs catalog) | local_match (local file vs catalog)
        kind     TEXT NOT NULL,
        strength TEXT NOT NULL,
        reason   TEXT NOT NULL
    );

    -- User decisions. Never derived, never dropped by rebuilds. No FK on purpose: a decision
    -- must survive a track temporarily leaving the crate.
    CREATE TABLE dedup_decisions (
        pair_key   TEXT PRIMARY KEY,
        uri_a      TEXT NOT NULL,
        uri_b      TEXT NOT NULL,
        -- same | different
        decision   TEXT NOT NULL,
        decided_at TEXT NOT NULL
    );
    """,
    # v3 - Phase 3: cache of independent descriptors (MusicBrainz, Last.fm)
    """
    -- Trimmed responses from external providers, keyed by what was asked. Descriptors are
    -- derived from this on demand, so recordings can be regrouped without refetching.
    CREATE TABLE metadata_cache (
        provider     TEXT NOT NULL,
        lookup_key   TEXT NOT NULL,
        -- ok | not_found
        status       TEXT NOT NULL,
        payload_json TEXT,
        fetched_at   TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        PRIMARY KEY (provider, lookup_key)
    );
    """,
    # v4 - Phase 3b: your descriptions of old playlists, playlist identities, proposals
    """
    -- What you say an old playlist was for. Evidence only: the new collection is built from
    -- scratch. User data; never derived.
    CREATE TABLE playlist_intents (
        playlist_id TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        intent      TEXT NOT NULL,
        -- sound | mood | situation | era | handpicked | source | catch_all | mixed | unknown
        kind        TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    -- A playlist the app will build and maintain (or, for hand-picked kinds, only hold).
    CREATE TABLE identities (
        identity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        kind          TEXT NOT NULL,
        concept       TEXT NOT NULL DEFAULT '',
        -- approved | rejected | retired
        status        TEXT NOT NULL,
        -- 0 = never add songs automatically (hand-picked, source-based)
        auto_maintain INTEGER NOT NULL,
        rules_json    TEXT NOT NULL,
        origin_json   TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );

    -- Songs you (or the seed playlist) say belong, never belong, or must stay. Keyed by URI
    -- so they survive duplicate regrouping; resolved to recordings when used.
    CREATE TABLE identity_examples (
        identity_id INTEGER NOT NULL REFERENCES identities(identity_id) ON DELETE CASCADE,
        uri         TEXT NOT NULL,
        -- reference | exclude | essential
        role        TEXT NOT NULL,
        -- seed | user
        source      TEXT NOT NULL,
        added_at    TEXT NOT NULL,
        PRIMARY KEY (identity_id, uri)
    );

    -- A proposed concept for the new collection, with the evidence behind it.
    CREATE TABLE proposals (
        proposal_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        -- from_description | from_library | handpicked
        action         TEXT NOT NULL,
        title          TEXT NOT NULL,
        playlists_json TEXT NOT NULL,
        evidence_json  TEXT NOT NULL,
        drafts_json    TEXT NOT NULL,
        -- pending | accepted | rejected | superseded
        status         TEXT NOT NULL,
        signature      TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        decided_at     TEXT
    );
    """,
    # v5 - Phase 4: draft playlists (derived; regenerated by `curator draft`)
    """
    CREATE TABLE draft_runs (
        run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at   TEXT NOT NULL,
        summary_json TEXT NOT NULL
    );

    CREATE TABLE draft_members (
        run_id        INTEGER NOT NULL REFERENCES draft_runs(run_id) ON DELETE CASCADE,
        identity_id   INTEGER NOT NULL,
        recording_id  TEXT NOT NULL,
        -- include | review
        decision      TEXT NOT NULL,
        score         REAL NOT NULL,
        tag_score     REAL,
        example_score REAL,
        -- song | artist | none: how specific the tag evidence was
        evidence      TEXT NOT NULL,
        reasons_json  TEXT NOT NULL,
        PRIMARY KEY (run_id, identity_id, recording_id)
    );
    CREATE INDEX idx_draft_members_identity ON draft_members(run_id, identity_id);
    """,
    # v6 - Phase 5: your decisions on songs, per playlist
    """
    -- Latest decision per playlist and song. User data; never derived.
    CREATE TABLE feedback (
        identity_id INTEGER NOT NULL REFERENCES identities(identity_id) ON DELETE CASCADE,
        uri         TEXT NOT NULL,
        -- keep | reject | essential | uncertain
        decision    TEXT NOT NULL,
        -- the example role this song had before your decision, restored on undo
        prev_role   TEXT,
        prev_source TEXT,
        -- draft score when you decided (for acceptance metrics), if it was in a draft
        score       REAL,
        -- include | review | none: where the draft had it when you decided
        draft_state TEXT NOT NULL,
        decided_at  TEXT NOT NULL,
        PRIMARY KEY (identity_id, uri)
    );

    -- Every decision ever made, for metrics over time. Append-only.
    CREATE TABLE feedback_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        identity_id INTEGER NOT NULL,
        uri         TEXT NOT NULL,
        decision    TEXT NOT NULL,
        draft_state TEXT NOT NULL,
        score       REAL,
        decided_at  TEXT NOT NULL
    );
    """,
    # v7 - Phase 6: playlists the app created in Spotify, snapshots for rollback, sync log
    """
    -- The ONLY Spotify playlists the app may change. One per playlist identity.
    CREATE TABLE managed_playlists (
        identity_id      INTEGER PRIMARY KEY REFERENCES identities(identity_id),
        spotify_id       TEXT NOT NULL UNIQUE,
        name             TEXT NOT NULL,
        description      TEXT,
        -- active | missing (deleted in Spotify) | released (you stopped syncing it)
        status           TEXT NOT NULL,
        last_snapshot_id TEXT,
        -- URIs as they were right after the app's last successful write; used to spot
        -- songs you added or removed yourself in Spotify since then
        last_uris_json   TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        last_synced_at   TEXT
    );

    -- State of a managed playlist before (and after) each change, for rollback.
    CREATE TABLE playlist_snapshots (
        snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        identity_id  INTEGER NOT NULL,
        spotify_id   TEXT NOT NULL,
        taken_at     TEXT NOT NULL,
        -- before_sync | after_sync | before_rollback | after_rollback
        reason       TEXT NOT NULL,
        uris_json    TEXT NOT NULL,
        sync_run_id  INTEGER
    );
    CREATE INDEX idx_snapshots_identity ON playlist_snapshots(identity_id, snapshot_id);

    CREATE TABLE sync_runs (
        run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        -- running | ok | partial | failed
        status       TEXT NOT NULL,
        summary_json TEXT,
        errors_json  TEXT
    );
    """,
]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode; multi-statement writes use the explicit ``transaction()`` helper.
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def schema_version(self) -> int:
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not exists:
            return 0
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 0

    def migrate(self) -> None:
        current = self.schema_version()
        for version, script in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            # executescript cannot take parameters; version is an int we control.
            self.conn.executescript(
                f"BEGIN;\n{script}\n"
                f"INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '{version}');\n"
                "COMMIT;"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group writes so an import either lands completely or not at all."""
        try:
            self.conn.execute("BEGIN")
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # -- tracks ------------------------------------------------------------

    def upsert_tracks(self, tracks: Iterable[Track], seen_at: str) -> None:
        rows = [
            (
                t.uri,
                t.spotify_id,
                t.name,
                json.dumps([a.model_dump() for a in t.artists]),
                t.album_name,
                t.album_id,
                t.album_type,
                t.release_date,
                t.release_date_precision,
                t.duration_ms,
                _bool(t.explicit),
                t.isrc,
                int(t.is_local),
                _bool(t.is_playable),
                t.disc_number,
                t.track_number,
                seen_at,
                seen_at,
            )
            for t in tracks
        ]
        self.conn.executemany(
            """
            INSERT INTO tracks (
                uri, spotify_id, name, artists_json, album_name, album_id, album_type,
                release_date, release_date_precision, duration_ms, explicit, isrc, is_local,
                is_playable, disc_number, track_number, first_seen_at, last_seen_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(uri) DO UPDATE SET
                spotify_id=excluded.spotify_id, name=excluded.name,
                artists_json=excluded.artists_json, album_name=excluded.album_name,
                album_id=excluded.album_id, album_type=excluded.album_type,
                release_date=excluded.release_date,
                release_date_precision=excluded.release_date_precision,
                duration_ms=excluded.duration_ms, explicit=excluded.explicit,
                isrc=COALESCE(excluded.isrc, tracks.isrc), is_local=excluded.is_local,
                is_playable=excluded.is_playable, disc_number=excluded.disc_number,
                track_number=excluded.track_number, last_seen_at=excluded.last_seen_at
            """,
            rows,
        )

    def get_track(self, uri: str) -> Track | None:
        row = self.conn.execute("SELECT * FROM tracks WHERE uri=?", (uri,)).fetchone()
        return _row_to_track(row) if row else None

    def iter_tracks(self) -> Iterator[Track]:
        for row in self.conn.execute("SELECT * FROM tracks ORDER BY uri"):
            yield _row_to_track(row)

    def referenced_track_uris(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT track_uri FROM source_entries")}

    def delete_orphan_tracks(self) -> int:
        cur = self.conn.execute(
            "DELETE FROM tracks WHERE uri NOT IN (SELECT DISTINCT track_uri FROM source_entries)"
        )
        return cur.rowcount

    # -- sources -----------------------------------------------------------

    def replace_source_entries(
        self, kind: SourceKind, source_id: str, entries: Iterable[SourceEntry]
    ) -> None:
        self.conn.execute(
            "DELETE FROM source_entries WHERE kind=? AND source_id=?", (kind.value, source_id)
        )
        self.conn.executemany(
            """INSERT INTO source_entries (track_uri, kind, source_id, position, added_at, added_by)
               VALUES (?,?,?,?,?,?)""",
            [
                (
                    e.track_uri,
                    e.kind.value,
                    e.source_id,
                    e.position,
                    e.added_at.isoformat() if e.added_at else None,
                    e.added_by,
                )
                for e in entries
            ],
        )

    def source_entries_for(self, uri: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT se.*, p.name AS playlist_name FROM source_entries se
               LEFT JOIN playlists p ON se.kind='playlist' AND p.spotify_id = se.source_id
               WHERE se.track_uri=? ORDER BY se.kind, p.name, se.position""",
            (uri,),
        ).fetchall()

    # -- playlists ---------------------------------------------------------

    def get_playlist_row(self, playlist_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM playlists WHERE spotify_id=?", (playlist_id,)
        ).fetchone()

    def upsert_playlist(
        self,
        summary: PlaylistSummary,
        *,
        owned_by_user: bool,
        contents_status: str,
        contents_note: str | None,
        fetched_snapshot_id: str | None,
        last_fetched_at: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO playlists (
                spotify_id, name, description, owner_id, owner_name, collaborative, public,
                snapshot_id, total_items, owned_by_user, contents_status, contents_note,
                fetched_snapshot_id, last_fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(spotify_id) DO UPDATE SET
                name=excluded.name, description=excluded.description,
                owner_id=excluded.owner_id, owner_name=excluded.owner_name,
                collaborative=excluded.collaborative, public=excluded.public,
                snapshot_id=excluded.snapshot_id, total_items=excluded.total_items,
                owned_by_user=excluded.owned_by_user, contents_status=excluded.contents_status,
                contents_note=excluded.contents_note,
                fetched_snapshot_id=excluded.fetched_snapshot_id,
                last_fetched_at=excluded.last_fetched_at
            """,
            (
                summary.spotify_id,
                summary.name,
                summary.description,
                summary.owner_id,
                summary.owner_name,
                int(summary.collaborative),
                _bool(summary.public),
                summary.snapshot_id,
                summary.total_items,
                int(owned_by_user),
                contents_status,
                contents_note,
                fetched_snapshot_id,
                last_fetched_at,
            ),
        )

    def delete_playlists_not_in(self, keep_ids: set[str]) -> list[str]:
        """Forget playlists that are no longer in your library (does not touch Spotify)."""
        existing = [r[0] for r in self.conn.execute("SELECT spotify_id FROM playlists")]
        gone = [pid for pid in existing if pid not in keep_ids]
        for pid in gone:
            self.conn.execute(
                "DELETE FROM source_entries WHERE kind='playlist' AND source_id=?", (pid,)
            )
            self.conn.execute("DELETE FROM playlists WHERE spotify_id=?", (pid,))
        return gone

    def list_playlists(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM source_entries se
                        WHERE se.kind='playlist' AND se.source_id=p.spotify_id) AS stored_entries
               FROM playlists p ORDER BY p.name COLLATE NOCASE"""
        ).fetchall()

    # -- import runs -------------------------------------------------------

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO import_runs (started_at, status) VALUES (?, 'running')", (utcnow(),)
        )
        return int(cur.lastrowid or 0)

    def finish_run(
        self, run_id: int, status: str, counts: ImportCounts, errors: list[dict[str, Any]]
    ) -> None:
        self.conn.execute(
            """UPDATE import_runs SET finished_at=?, status=?, counts_json=?, errors_json=?
               WHERE id=?""",
            (utcnow(), status, counts.model_dump_json(), json.dumps(errors), run_id),
        )

    def last_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM import_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def _row_to_track(row: sqlite3.Row) -> Track:
    def opt_bool(v: Any) -> bool | None:
        return None if v is None else bool(v)

    return Track(
        uri=row["uri"],
        spotify_id=row["spotify_id"],
        name=row["name"],
        artists=tuple(ArtistRef(**a) for a in json.loads(row["artists_json"])),
        album_name=row["album_name"],
        album_id=row["album_id"],
        album_type=row["album_type"],
        release_date=row["release_date"],
        release_date_precision=row["release_date_precision"],
        duration_ms=row["duration_ms"],
        explicit=opt_bool(row["explicit"]),
        isrc=row["isrc"],
        is_local=bool(row["is_local"]),
        is_playable=opt_bool(row["is_playable"]),
        disc_number=row["disc_number"],
        track_number=row["track_number"],
    )


def purge_data(data_dir: Path) -> list[Path]:
    """Delete the local database and token cache. Used for 'disconnect my account'."""
    removed: list[Path] = []
    for name in ("curator.sqlite3", "curator.sqlite3-wal", "curator.sqlite3-shm", ".tokens.json"):
        p = data_dir / name
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed
