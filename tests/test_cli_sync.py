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


WRITE = "user-library-read playlist-read-private playlist-modify-private playlist-modify-public"


def test_cover_preview_needs_permission_and_only_touches_app_playlists(env, fake, capsys):
    save_tokens(env, WRITE)
    assert cli.main(["sync", "--apply", "--yes"]) == 0
    capsys.readouterr()
    assert not any(p.endswith("/images") for _, p, _ in fake.write_bodies)  # old login: skipped

    assert cli.main(["sync", "cover", "Night Drive", "--preview"]) == 0
    assert (env / "data" / "covers" / "Night_Drive.jpg").stat().st_size > 1000
    assert not any(p.endswith("/images") for _, p, _ in fake.write_bodies)

    assert cli.main(["sync", "cover", "Night Drive", "--yes"]) == 1
    assert "one more permission" in capsys.readouterr().err

    save_tokens(env, WRITE + " ugc-image-upload")
    assert cli.main(["sync", "cover", "--all", "--yes"]) == 0
    puts = [p for m, p, _ in fake.write_bodies if p.endswith("/images")]
    assert len(puts) == 1 and puts[0].startswith("/playlists/pl_new")
    sid = puts[0].split("/")[2]
    import base64

    assert base64.b64decode(fake.playlist(sid)["cover_b64"])[:2] == b"\xff\xd8"  # a JPEG


def test_own_image_is_cropped_square(env, fake, capsys, tmp_path):
    from PIL import Image

    from playlist_curator import covers

    pic = tmp_path / "wide.png"
    Image.new("RGB", (1600, 900), (10, 200, 30)).save(pic)
    img = covers.from_file(pic)
    assert img.size == (640, 640)
    # never stretched: a small image keeps its own size; focus picks the part kept
    Image.new("RGB", (300, 200), (0, 0, 0)).save(small := tmp_path / "small.png")
    assert covers.from_file(small).size == (200, 200)
    half = Image.new("RGB", (400, 200), (255, 0, 0))
    half.paste((0, 0, 255), (200, 0, 400, 200))
    half.save(split := tmp_path / "split.png")
    assert covers.from_file(split, 0.0).getpixel((100, 100)) == (255, 0, 0)
    assert covers.from_file(split, 1.0).getpixel((100, 100)) == (0, 0, 255)
    assert (
        cli.main(
            ["sync", "cover", "Night Drive", "--image", str(pic), "--focus", "0.3", "--preview"]
        )
        == 0
    )
    assert "your image" in capsys.readouterr().out


def test_new_playlists_get_a_cover_when_permission_is_there(env, fake, capsys):
    save_tokens(env, WRITE + " ugc-image-upload")
    assert cli.main(["sync", "--apply", "--yes"]) == 0
    assert "cover set for 'Night Drive'" in capsys.readouterr().out
    assert sum(p.endswith("/images") for _, p, _ in fake.write_bodies) == 1
