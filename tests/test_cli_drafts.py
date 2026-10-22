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


def test_review_feedback_decisions_stats(env, capsys, monkeypatch):
    assert (
        cli.main(["describe", "chill indie", "--like", "chill artist 1", "--create", "Chill"]) == 0
    )
    assert cli.main(["draft"]) == 0
    capsys.readouterr()

    answers = iter(["x", "k", "r", "e", "u", "s", "q"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    assert cli.main(["review", "Chill", "--all"]) == 0
    out = capsys.readouterr().out
    assert "[k]eep" in out and "Saved: 1 keep, 1 reject, 1 essential, 1 uncertain" in out

    assert cli.main(["feedback", "Chill", "reject", "Jazz Song 3", "Nope Song"]) == 1
    out = capsys.readouterr().out
    assert "reject: Jazz Song 3" in out and "! No song called 'Nope Song'" in out
    assert cli.main(["feedback", "Chill", "undo", "Jazz Song 3"]) == 0
    assert "undone: Jazz Song 3" in capsys.readouterr().out

    assert cli.main(["decisions", "Chill"]) == 0
    assert "keep" in capsys.readouterr().out
    assert cli.main(["draft"]) == 0
    assert "Decide song by song" in capsys.readouterr().out
    assert cli.main(["stats", "--evaluate"]) == 0
    out = capsys.readouterr().out
    assert "accept%" in out and "holdout" in out and "Chill" in out
    assert cli.main(["review", "Nobody"]) == 1


def test_tags_commands(env, capsys):
    assert cli.main(["tags"]) == 0
    out = capsys.readouterr().out
    assert "vibe (" in out and "genre (" in out
    assert cli.main(["tags", "songs", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "judged by their own data" in out and "Showing 3" in out
    assert cli.main(["tags", "song", "Chill Song 1"]) == 0
    assert "sounds:" in capsys.readouterr().out
    assert cli.main(["tags", "set", "indie", "vibe"]) == 0
    assert "now read as: vibe" in capsys.readouterr().out
