import pytest

from playlist_curator import cli
from playlist_curator.crate.importer import import_library
from playlist_curator.curator import identities as idn
from playlist_curator.curator import scoring as sc
from playlist_curator.spotify.auth import TokenSet, TokenStore
from playlist_curator.storage.database import Database, utcnow
from test_sync import descriptors


@pytest.fixture
def env(tmp_path, monkeypatch, fake, client):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-123")
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "data"))
    import playlist_curator.cli_sync as cs

    monkeypatch.setattr(cs, "_client", lambda settings: client)
    with Database(tmp_path / "data" / "curator.sqlite3") as db:
        import_library(client, db, progress=lambda _m: None)
        night = idn.create_identity(db, "Night Drive")
        idn.edit_identity(db, night.identity_id, add_tags={"synthwave": 1.0})
        db.conn.execute(
            "INSERT INTO identity_examples VALUES (?,?,'reference','user',?)",
            (night.identity_id, "spotify:track:trk_midnight", utcnow()),
        )
        sc.build_drafts(db, descriptors=descriptors(db))
    return tmp_path


def save_tokens(env, scope):
    TokenStore(env / "data" / ".tokens.json").save(TokenSet("a", "r", 9e12, scope=scope))


def test_preview_then_apply_with_confirmation(env, fake, capsys, monkeypatch):
    assert cli.main(["sync", "--songs"]) == 0
    out = capsys.readouterr().out
    assert "FIRST SYNC" in out and "NEW (private)" in out and "Preview only" in out
    assert "+ Midnight City" in out
    assert fake.write_bodies == []

    save_tokens(env, "user-library-read playlist-read-private")
    assert cli.main(["sync", "--apply"]) == 1
    assert "read-only" in capsys.readouterr().err

    save_tokens(
        env,
        "user-library-read playlist-read-private playlist-modify-private playlist-modify-public",
    )
    monkeypatch.setattr("builtins.input", lambda _p: "no")
    assert cli.main(["sync", "--apply"]) == 1
    assert fake.write_bodies == []
    monkeypatch.setattr("builtins.input", lambda _p: "yes")
    assert cli.main(["sync", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "Sync ok: 1 playlist(s) written and verified" in out
    assert "curator sync local-files" in out

    assert cli.main(["sync"]) == 0
    assert "Nothing to change" in capsys.readouterr().out
    assert cli.main(["sync", "managed"]) == 0
    assert "open.spotify.com/playlist/" in capsys.readouterr().out
    assert cli.main(["sync", "local-files"]) == 0
    assert "□ Song Title: Night Remix" in capsys.readouterr().out
    assert cli.main(["sync", "history", "Night Drive"]) == 0
    assert "before_sync" in capsys.readouterr().out
    assert cli.main(["rollback", "Night Drive", "--yes"]) == 0
    assert "Restored 'Night Drive'" in capsys.readouterr().out
    assert cli.main(["sync", "release", "Night Drive"]) == 0
    assert cli.main(["sync", "managed"]) == 0
