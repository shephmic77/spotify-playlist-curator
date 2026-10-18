import pytest

from playlist_curator import cli
from playlist_curator.curator import feedback as fb
from playlist_curator.curator import identities as idn
from playlist_curator.curator import maintenance as mt
from playlist_curator.spotify import sync
from test_sync import descriptors, setup  # noqa: F401  (setup is a fixture)


def run(db, client, **kw):
    return mt.run(db, client, descriptors=descriptors(db), **kw)


def first_sync(db, client):
    return sync.apply(db, client, sync.plan(db, client)).applied[0]["spotify_id"]


def night(db):
    return idn.get_identity(db, "Night Drive")


@pytest.mark.usefixtures("setup")
def test_new_playlists_are_never_created_automatically(fake, client, db):
    mt.save_settings(db, auto_apply=True)
    res = run(db, client)
    assert res.status == "held" and res.mode == "auto"
    assert res.held[0]["playlist"] == "Night Drive" and "new playlist" in res.held[0]["reason"]
    assert fake.write_bodies == [] and res.applied == []
    assert mt.last_runs(db)[0]["status"] == "held"
    assert "Waiting for your approval" in mt.report(res)


@pytest.mark.usefixtures("setup")
def test_preview_by_default_then_auto_applies_small_changes(fake, client, db):
    sid = first_sync(db, client)
    fb.decide_uri(db, night(db), "spotify:track:trk_africa", "essential")
    writes = len(fake.write_bodies)

    res = run(db, client)  # auto-apply is off by default
    assert res.mode == "preview" and res.status == "ok"
    assert res.ready == [
        {
            "playlist": "Night Drive",
            "action": "update",
            "add": 1,
            "remove": 0,
            "now": 2,
            "manual_edits": 0,
        }
    ]
    assert len(fake.write_bodies) == writes
    assert "curator sync --apply" in mt.report(res)

    mt.save_settings(db, auto_apply=True)
    res = run(db, client, trigger="scheduled")
    assert res.applied and res.applied[0]["added"] == 1
    assert "spotify:track:trk_africa" in fake.playlist(sid)["entries"]
    touched = {p for _m, p, _b in fake.write_bodies[writes:]}
    assert touched and all(sid in p for p in touched)  # only the app's own playlist
    assert [r["trigger"] for r in mt.last_runs(db)][:1] == ["scheduled"]
    # running again finds nothing to do
    assert run(db, client).ready == []


@pytest.mark.usefixtures("setup")
def test_big_changes_wait_for_you(fake, client, db):
    first_sync(db, client)
    fb.decide_uri(db, night(db), "spotify:track:trk_africa", "essential")
    mt.save_settings(db, auto_apply=True, max_changes=0, max_change_share=0.0)
    writes = len(fake.write_bodies)
    res = run(db, client)
    assert res.status == "held" and "1 songs would change (limit 0)" in res.held[0]["reason"]
    assert len(fake.write_bodies) == writes


@pytest.mark.usefixtures("setup")
def test_library_shrinking_holds_everything(fake, client, db):
    first_sync(db, client)
    mt.save_settings(db, auto_apply=True)
    fake.lib["liked"] = []
    for p in fake.lib["playlists"]:
        if p["id"] in ("pl_80s", "pl_synth"):
            p["entries"] = []
            p["snapshot_id"] = "changed"
    writes = len(fake.write_bodies)
    res = run(db, client)
    assert any("library shrank" in e for e in res.errors)
    assert res.held and all(h["reason"].startswith("library data") for h in res.held)
    assert len(fake.write_bodies) == writes


@pytest.mark.usefixtures("setup")
def test_your_spotify_edits_are_kept_by_maintenance(fake, client, db):
    sid = first_sync(db, client)
    fake.playlist(sid)["entries"].append("spotify:track:trk_clair")
    mt.save_settings(db, auto_apply=True)
    run(db, client)
    decisions = {d["uri"]: d["decision"] for d in fb.decisions_for(db, night(db).identity_id)}
    assert decisions["spotify:track:trk_clair"] == "keep"
    assert "spotify:track:trk_clair" in fake.playlist(sid)["entries"]


@pytest.mark.usefixtures("setup")
def test_failures_are_recorded(fake, client, db):
    fake.inject("/me", 400, body={"error": {"status": 400, "message": "boom"}})
    with pytest.raises(Exception, match="boom"):
        run(db, client)
    r = mt.last_runs(db)[0]
    assert r["status"] == "failed" and "boom" in r["errors"][0]


def test_settings_roundtrip_and_validation(db):
    assert mt.load_settings(db).auto_apply is False
    cfg = mt.save_settings(db, auto_apply=True, max_changes=5)
    assert cfg.auto_apply and cfg.max_changes == 5 and cfg.max_change_share == 0.25
    assert mt.load_settings(db).max_changes == 5
    assert cfg.limit_for(100) == 25 and cfg.limit_for(4) == 5
    with pytest.raises(ValueError):
        mt.save_settings(db, max_change_share=2)


def test_schtasks_command_quotes_paths_with_spaces(tmp_path):
    from playlist_curator import cli_maintain as cm

    w = tmp_path / "Data Science projects" / "run.cmd"
    args = cm.schtasks_args(w, "weekly", "08:30")
    assert args[args.index("/TR") + 1] == f'"{w}"'
    assert args[-4:] == ["/SC", "WEEKLY", "/D", "SUN"] and "08:30" in args


@pytest.fixture
def env(tmp_path, monkeypatch, fake, client):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SPOTIFY_CLIENT_ID=client-123\n")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-123")
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "data"))
    import playlist_curator.cli_maintain as cm

    monkeypatch.setattr(cm, "SpotifyClient", lambda _tm: client)
    monkeypatch.setattr(cm, "_providers", lambda db, s: [])
    assert cli.main(["import"]) in (0, 1)  # creates the database (import may need login)
    return tmp_path


def test_cli_maintain_status_settings_schedule(env, fake, client, capsys, monkeypatch):
    from playlist_curator.crate.importer import import_library
    from playlist_curator.storage.database import Database

    with Database(env / "data" / "curator.sqlite3") as db:
        import_library(client, db, progress=lambda _m: None)
        idn.create_identity(db, "Night Drive")
    capsys.readouterr()
    assert cli.main(["maintain", "--no-enrich"]) == 0
    out = capsys.readouterr().out
    assert "1/4 Importing" in out and "preview only" in out
    assert (env / "data" / "reports" / "latest-maintenance.txt").exists()
    assert fake.write_bodies == []

    assert cli.main(["maintain", "settings", "--auto-apply", "on", "--max-changes", "3"]) == 0
    assert "Automatic updates: on" in capsys.readouterr().out
    # read-only login: falls back to a preview
    from playlist_curator.spotify.auth import TokenSet, TokenStore

    TokenStore(env / "data" / ".tokens.json").save(
        TokenSet("a", "r", 9e12, scope="user-library-read playlist-read-private")
    )
    assert cli.main(["maintain", "--quiet"]) == 0
    captured = capsys.readouterr()
    assert "preview only" in captured.out and "can't edit playlists" in captured.err

    assert cli.main(["maintain", "status"]) == 0
    out = capsys.readouterr().out
    assert "Automatic updates: on" in out and "#2" in out

    monkeypatch.setattr("sys.platform", "linux")
    assert cli.main(["maintain", "schedule", "--at", "7:05"]) == 0
    assert "5 7 * * *" in capsys.readouterr().out
