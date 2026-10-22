import pytest

from playlist_curator import cli
from playlist_curator.crate.importer import import_library
from playlist_curator.curator import identities as idn
from playlist_curator.spotify import sync
from playlist_curator.storage.database import Database
from test_sync import setup  # noqa: F401  (fixture)


def first_sync(db, client):
    return sync.apply(db, client, sync.plan(db, client)).applied[0]["spotify_id"]


def test_client_refuses_anything_but_playlist_ids(client):
    for bad in ("spotify:track:trk_midnight", "trk:x", "a,b", ""):
        with pytest.raises(ValueError):
            client.remove_playlists_from_library([bad])


@pytest.mark.usefixtures("setup")
def test_start_over_removes_only_app_playlists_and_wipes_definitions(fake, client, db, tmp_path):
    sid = first_sync(db, client)
    liked_before = list(fake.lib["liked"])
    own_before = {p["id"] for p in fake.lib["playlists"]} - {sid}

    out = sync.start_over(db, client, backup_dir=tmp_path / "backups")

    removals = [b for m, p, b in fake.write_bodies if p == "/me/library"]
    assert removals == [{"uris": [f"spotify:playlist:{sid}"]}]
    assert {p["id"] for p in fake.lib["playlists"]} == own_before  # yours untouched
    assert fake.lib["liked"] == liked_before
    assert out["removed"] == [sid] and out["failed"] == []
    assert out["wiped"]["identities"] == 1
    for t in sync.WIPED_TABLES:
        assert db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] > 0  # library kept

    with Database(out["backup"]) as old:  # everything is in the backup
        assert old.conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0] == 1
        assert old.conn.execute("SELECT COUNT(*) FROM managed_playlists").fetchone()[0] == 1


@pytest.mark.usefixtures("setup")
def test_a_playlist_that_couldnt_be_removed_is_never_imported_as_yours(fake, client, db, tmp_path):
    sid = first_sync(db, client)
    fake.inject("/me/library", 403, body={"error": {"status": 403, "message": "nope"}})
    out = sync.start_over(db, client, backup_dir=tmp_path)
    assert out["failed"][0]["spotify_id"] == sid
    assert any(p["id"] == sid for p in fake.lib["playlists"])  # still in Spotify
    import_library(client, db, progress=lambda _m: None)
    assert db.get_playlist_row(sid) is None


@pytest.fixture
def env(tmp_path, monkeypatch, fake, client):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-123")
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "data"))
    import playlist_curator.cli_sync as cs

    monkeypatch.setattr(cs, "_client", lambda settings: client)
    return tmp_path


def test_cli_start_over_needs_typed_confirmation(env, fake, client, capsys, monkeypatch):
    from playlist_curator.spotify.auth import TokenSet, TokenStore

    with Database(env / "data" / "curator.sqlite3") as db:
        import_library(client, db, progress=lambda _m: None)
        idn.create_identity(db, "Old one")
    TokenStore(env / "data" / ".tokens.json").save(
        TokenSet("a", "r", 9e12, scope="playlist-modify-private playlist-modify-public")
    )
    monkeypatch.setattr("builtins.input", lambda _p: "yes")
    assert cli.main(["start-over"]) == 1
    assert "Cancelled" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _p: "start over")
    assert cli.main(["start-over"]) == 0
    out = capsys.readouterr().out
    assert "wiped 1 definition(s)" in out and "curator describe" in out
    assert list((env / "data" / "backups").glob("curator-before-start-over-*.sqlite3"))
