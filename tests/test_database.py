import sqlite3

from fake_spotify import load_library
from playlist_curator.models import ImportCounts, SourceEntry, SourceKind, parse_track
from playlist_curator.storage.database import MIGRATIONS, Database, purge_data


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "db.sqlite3"
    Database(path).close()
    with Database(path) as db:
        assert db.schema_version() == len(MIGRATIONS)


def test_track_round_trip(db):
    t = parse_track(load_library()["tracks"]["nightcall"]).track
    db.upsert_tracks([t], seen_at="2026-01-01T00:00:00+00:00")
    assert db.get_track(t.uri) == t


def test_upsert_does_not_erase_known_isrc(db):
    t = parse_track(load_library()["tracks"]["midnight_city"]).track
    db.upsert_tracks([t], seen_at="a")
    db.upsert_tracks([t.model_copy(update={"isrc": None})], seen_at="b")
    assert db.get_track(t.uri).isrc == "FRZID1100001"


def test_transaction_rolls_back_on_error(db):
    t = parse_track(load_library()["tracks"]["africa"]).track
    try:
        with db.transaction():
            db.upsert_tracks([t], seen_at="a")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.get_track(t.uri) is None


def test_source_entries_require_known_track(db):
    entry = SourceEntry(
        track_uri="spotify:track:missing", kind=SourceKind.LIKED, source_id="__liked__", position=0
    )
    try:
        db.replace_source_entries(SourceKind.LIKED, "__liked__", [entry])
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised


def test_run_log(db):
    run_id = db.start_run()
    db.finish_run(run_id, "ok", ImportCounts(liked_entries=3), [])
    row = db.last_runs(1)[0]
    assert row["status"] == "ok" and row["finished_at"]


def test_purge_removes_db_and_tokens(tmp_path):
    Database(tmp_path / "curator.sqlite3").close()
    (tmp_path / ".tokens.json").write_text("{}")
    removed = purge_data(tmp_path)
    assert {p.name for p in removed} >= {"curator.sqlite3", ".tokens.json"}
    assert not (tmp_path / ".tokens.json").exists()
