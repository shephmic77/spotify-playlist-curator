import json

import pytest

from playlist_curator import cli


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray .env picked up
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-123")
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_import_inventory_playlists_purge(env, client, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda settings: client)

    assert cli.main(["import"]) == 0
    out = capsys.readouterr().out
    assert "Import ok" in out
    assert "Unique recordings" in out
    assert "contents unavailable via API" in out

    assert cli.main(["inventory", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["unique_tracks"] == 11
    assert data["last_run"]["status"] == "ok"

    assert cli.main(["playlists"]) == 0
    out = capsys.readouterr().out
    assert "[x] Today's Top Hits" in out
    assert "owner: you" in out

    assert cli.main(["purge", "--yes"]) == 0
    assert not (env / "curator.sqlite3").exists()


def test_inventory_before_import(env, capsys):
    assert cli.main(["inventory"]) == 1
    assert "curator import" in capsys.readouterr().out


def test_missing_client_id_is_a_clear_error(env, monkeypatch, capsys):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "")
    assert cli.main(["import"]) == 1
    assert "SPOTIFY_CLIENT_ID" in capsys.readouterr().err


def test_not_logged_in_is_a_clear_error(env, capsys):
    assert cli.main(["import"]) == 1
    assert "curator auth login" in capsys.readouterr().err


def test_duplicates_review_list_undo_and_local_files(env, client, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda settings: client)
    assert cli.main(["import"]) == 0
    out = capsys.readouterr().out
    assert "possible duplicate(s) need a decision" in out

    # local_match is listed before title_match: accept the local file, reject the remaster
    answers = iter(["x", "s", "d"])  # "x" is invalid and re-prompts
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli.main(["duplicates", "review"]) == 0
    out = capsys.readouterr().out
    assert "Saved 2 decision(s)" in out
    assert "local_match" in out and "title_match" in out

    assert cli.main(["duplicates", "review"]) == 0
    assert "Nothing to review" in capsys.readouterr().out

    assert cli.main(["duplicates", "list"]) == 0
    out = capsys.readouterr().out
    assert "Take On Me" in out and "Nightcall" in out

    assert cli.main(["local-files"]) == 0
    out = capsys.readouterr().out
    assert "= Nightcall" in out
    assert "unmatched" in out

    assert cli.main(["duplicates", "decisions"]) == 0
    out = capsys.readouterr().out
    decision_id = out.split()[0]
    assert cli.main(["duplicates", "undo", decision_id]) == 0
    assert cli.main(["duplicates", "same", "zzzzzzzz"]) == 1


def test_enrich_and_analyze_commands(env, client, monkeypatch, capsys):
    from fake_metadata import FakeMetadataAPIs
    from playlist_curator.metadata import providers as prov

    apis = FakeMetadataAPIs()
    monkeypatch.setattr(cli, "_client", lambda settings: client)
    monkeypatch.setenv("LASTFM_API_KEY", "KEY")

    class FastMB(prov.MusicBrainzProvider):
        def __init__(self, db, http, **kw):
            super().__init__(db, apis.http(), min_interval=0)

    class FastLFM(prov.LastfmProvider):
        def __init__(self, db, http, key, **kw):
            super().__init__(db, apis.http(), key, min_interval=0)

    class FastAB(prov.AcousticBrainzProvider):
        def __init__(self, db, http, **kw):
            super().__init__(db, apis.http(), min_interval=0)

    monkeypatch.setattr(cli, "MusicBrainzProvider", FastMB)
    monkeypatch.setattr(cli, "LastfmProvider", FastLFM)
    monkeypatch.setattr(cli, "AcousticBrainzProvider", FastAB)

    assert cli.main(["import"]) == 0
    capsys.readouterr()
    assert cli.main(["analyze"]) == 0
    assert "No tags yet" in capsys.readouterr().out

    assert cli.main(["enrich"]) == 0
    out = capsys.readouterr().out
    assert "musicbrainz: 3 of 10 recordings have tags" in out
    assert "Coverage:" in out and "Last.fm" in out

    assert cli.main(["analyze"]) == 0
    out = capsys.readouterr().out
    assert "=== 80s" in out and "tagged songs:" in out
    # the 4-song fixture playlists are too small for a sound profile; say so instead
    assert "too few tagged songs" in out
    assert "Overlapping playlists" in out
    assert cli.main(["analyze", "--playlist", "nothing-like-this"]) == 1
    assert cli.main(["analyze", "--json"]) == 0


def test_enrich_without_lastfm_key_uses_musicbrainz_only(env, client, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda settings: client)
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)
    assert cli.main(["import"]) == 0
    capsys.readouterr()
    assert cli.main(["enrich", "--source", "lastfm"]) == 1
    assert "LASTFM_API_KEY" in capsys.readouterr().err
