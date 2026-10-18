import json

import pytest

from library_builder import build_library
from playlist_curator import cli
from playlist_curator.curator import discovery as disc
from playlist_curator.storage.database import Database


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-123")
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "data"))
    with Database(tmp_path / "data" / "curator.sqlite3") as db:
        desc = build_library(db)
    monkeypatch.setattr(disc, "load_descriptors", lambda db: desc)
    return tmp_path


def test_describe_propose_accept_edit(env, capsys):
    assert (
        cli.main(["intents", "set", "U", "indie chill", "--kind", "mood", "--title", "Indie chill"])
        == 0
    )
    assert cli.main(["intents", "set", "Nope", "x", "--kind", "mood"]) == 1
    (env / "intents.json").write_text(
        json.dumps(
            [
                {"playlist": "Ñ", "title": "Indie chill", "intent": "indie chill", "kind": "mood"},
                {"playlist": "W", "title": "Workout", "intent": "gym", "kind": "situation"},
                {"playlist": "RS", "title": "Regular Show", "intent": "show", "kind": "handpicked"},
            ]
        )
    )
    assert cli.main(["intents", "import", str(env / "intents.json")]) == 0
    capsys.readouterr()
    assert cli.main(["intents", "list"]) == 0
    out = capsys.readouterr().out
    assert "Workout" in out and "(no description)" in out  # e.g. 80s isn't described

    assert cli.main(["propose"]) == 0
    out = capsys.readouterr().out
    assert "Indie chill  (from your descriptions)" in out
    assert "hand-picked (optional)" in out
    assert "found in your library" in out

    with Database(env / "data" / "curator.sqlite3") as db:
        ids = {p.title: p.proposal_id for p in disc.load_proposals(db)}
    assert cli.main(["proposals", "accept", str(ids["Indie chill"]), "--name", "Chill"]) == 0
    assert cli.main(["proposals", "reject", "regular show"]) == 0  # by name
    assert cli.main(["proposals", "accept", "99999"]) == 1
    capsys.readouterr()
    assert cli.main(["proposals", "--all"]) == 0
    out = capsys.readouterr().out
    assert "accepted" in out and "rejected" in out

    assert cli.main(["identities"]) == 0
    assert "Chill  (mood)" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "identities",
                "edit",
                "Chill",
                "--add-tag",
                "lo-fi=0.5",
                "--years",
                "2010-2020",
                "--concept",
                "late night indie",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "lo-fi=0.5" in out and "2010–2020" in out
    assert cli.main(["identities", "show", "Chill", "--examples", "3"]) == 0
    assert out.count("·") == 0 and "late night indie" in out
    out = capsys.readouterr().out
    assert out.count("·") == 3
    assert (
        cli.main(
            ["identities", "new", "Classical", "--like", "Chill", "--concept", "only classical"]
        )
        == 0
    )
    assert cli.main(["identities", "edit", "Classical", "--require-tag", "classical"]) == 0
    assert "must have one of: classical" in capsys.readouterr().out
    assert cli.main(["identities", "status", "Chill", "retired"]) == 0
    assert cli.main(["identities", "show", "Nothing"]) == 1
