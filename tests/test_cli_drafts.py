import pytest

from library_builder import add_playlist, build_library
from playlist_curator import cli
from playlist_curator.curator import discovery as disc
from playlist_curator.curator import scoring as sc
from playlist_curator.storage.database import Database


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-123")
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with Database(tmp_path / "data" / "curator.sqlite3") as db:
        desc = build_library(db)
        add_playlist(db, "pl_v", "V", [("spotify:track:luv3", "Luv(sic) Part 3", "Nujabes", 2003)])
    import playlist_curator.cli_drafts as cd

    monkeypatch.setattr(cd, "load_descriptors", lambda db: desc)
    monkeypatch.setattr(sc, "load_descriptors", lambda db: desc)
    monkeypatch.setattr(disc, "load_descriptors", lambda db: desc)
    return tmp_path


def test_describe_preview_then_create_then_draft(env, capsys):
    text = "chill indie, lyrics are fine, not too loud"
    assert (
        cli.main(
            [
                "describe",
                text,
                "--like",
                "chill artist 1",
                "--like",
                "luvsic",
                "--like",
                "nope nope",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "engine: offline" in out
    assert "✓ chill artist 1 (artist" in out and "✓ luvsic (song, 1 song(s))" in out
    assert "✗ nope nope: not found in your library" in out
    assert "Add --create" in out
    # a description the dictionary doesn't know still works from examples alone
    assert cli.main(["describe", "my late night stuff", "--like", "chill artist 1"]) == 0
    out = capsys.readouterr().out
    assert "added from what your examples have in common: indie" in out

    assert cli.main(["describe", text, "--like", "chill artist 1", "--create", "Study"]) == 0
    assert "Created playlist" in capsys.readouterr().out

    assert cli.main(["draft"]) == 0
    out = capsys.readouterr().out
    assert "Nothing was sent to Spotify" in out and "Study" in out
    assert "Songs in no playlist" in out

    assert cli.main(["drafts"]) == 0
    assert "Study (" in capsys.readouterr().out
    assert cli.main(["drafts", "show", "Study", "--why", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "not probabilities" in out and "tags:" in out
    assert cli.main(["drafts", "unassigned", "--limit", "5"]) == 0
    assert "aren't in any draft playlist" in capsys.readouterr().out


def test_draft_needs_playlists_and_claude_needs_a_key(env, capsys):
    assert cli.main(["draft"]) == 1
    assert cli.main(["drafts"]) == 1
    assert cli.main(["describe", "study", "--engine", "claude"]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
