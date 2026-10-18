import pytest

from playlist_curator.crate.importer import AccountMismatchError, import_library
from playlist_curator.crate.inventory import playlist_table, summarize, track_table
from playlist_curator.spotify.client import RateLimitedError


def quiet(_msg: str) -> None:
    return None


def run(client, db, **kw):
    return import_library(client, db, progress=quiet, **kw)


def test_full_import_builds_crate_with_provenance(fake, client, db, small_pages):
    result = run(client, db)
    assert result.status == "ok"
    c = result.counts
    assert c.liked_entries == 3  # one null entry skipped
    assert c.playlists_seen == 6
    assert c.playlists_fetched == 5
    assert c.playlists_contents_unavailable == 1
    assert c.skipped == {"unavailable": 2, "episode": 1}

    s = summarize(db)
    # midnight, tom_album, tom_comp, tom_mtv, africa, africa_rm, nightcall, clair,
    # + 3 local files
    assert s.unique_tracks == 11
    assert s.local_tracks == 3
    assert s.catalog_tracks == 8
    assert s.in_liked == 3
    assert s.liked_only == 1  # Clair de lune is liked but in no playlist
    assert s.in_both == 2  # Midnight City, Take On Me (album)
    assert s.playlists_only == 8
    assert s.repeated_within_playlist == 2  # local_remix twice, africa twice
    assert s.isrc_groups_with_multiple_uris == 1  # album + compilation Take On Me
    assert s.tracks_in_isrc_groups == 2
    assert s.catalog_missing_isrc == 1  # Nightcall
    assert s.local_file_playlists == ["Local Files"]


def test_playlist_only_songs_are_in_the_crate(fake, client, db):
    run(client, db)
    rows = {r["uri"]: r for r in track_table(db)}
    africa = rows["spotify:track:trk_africa"]
    assert not africa["liked"]
    assert africa["playlists"] == ["80s", "Road Trip (collab)"]
    assert rows["spotify:track:trk_midnight"]["playlists"] == [
        "80s",
        "Synth, Neon & Night Drives",
    ]


def test_live_version_is_not_merged_with_studio_version(fake, client, db):
    run(client, db)
    uris = {r["uri"] for r in track_table(db)}
    assert {
        "spotify:track:trk_tom_album",
        "spotify:track:trk_tom_comp",
        "spotify:track:trk_tom_mtv",
    } <= uris


def test_local_entries_are_kept_with_source(fake, client, db):
    run(client, db)
    local = [r for r in track_table(db) if r["local_file"]]
    assert {r["name"] for r in local} == {
        "Song Title: Night Remix",
        "untitled bootleg",
        "Kavinsky - Nightcall (Official Video)",
    }
    assert all(r["playlists"] == ["Local Files"] for r in local)
    t = db.get_track("spotify:local:Some+Artist:YouTube+Rips:Song+Title%3A+Night+Remix:215")
    assert t is not None and t.spotify_id is None and t.is_local


def test_followed_playlist_recorded_as_unavailable_not_failed(fake, client, db):
    run(client, db)
    rows = {r["id"]: r for r in playlist_table(db)}
    assert rows["pl_followed"]["contents"] == "unavailable"
    assert "own or collaborate" in rows["pl_followed"]["note"]
    assert rows["pl_collab"]["contents"] == "fetched"
    assert rows["pl_collab"]["owned"] is False
    assert rows["pl_empty"]["contents"] == "fetched"
    assert rows["pl_empty"]["imported_entries"] == 0


def test_unchanged_snapshots_are_not_refetched(fake, client, db):
    run(client, db)
    first = fake.count("/playlists/")
    result = run(client, db)
    assert fake.count("/playlists/") == first  # no playlist item calls on second run
    assert result.counts.playlists_unchanged == 6
    assert result.counts.new_tracks == 0 and result.counts.removed_tracks == 0
    assert summarize(db).unique_tracks == 11


def test_force_refetches_everything(fake, client, db):
    run(client, db)
    before = fake.count("/playlists/")
    run(client, db, force=True)
    assert fake.count("/playlists/") == before * 2


def test_changed_playlist_is_refetched_and_diff_reported(fake, client, db):
    run(client, db)
    p = fake.playlist("pl_80s")
    p["entries"].remove("nightcall")
    p["entries"].append("clair_de_lune")
    p["snapshot_id"] = "snap_80s_v2"
    # Nightcall is still in the Synth playlist, so it must stay in the crate.
    result = run(client, db)
    assert result.counts.playlists_fetched == 1
    assert result.counts.new_tracks == 0 and result.counts.removed_tracks == 0

    fake.playlist("pl_synth")["entries"].remove("nightcall")
    fake.playlist("pl_synth")["snapshot_id"] = "snap_synth_v2"
    fake.lib["tracks"]["new_song"] = {
        **fake.lib["tracks"]["africa"],
        "id": "trk_new",
        "uri": "spotify:track:trk_new",
        "name": "New Song",
    }
    fake.lib["liked"].insert(0, {"added_at": "2026-10-17T00:00:00Z", "track": "new_song"})
    result = run(client, db)
    assert result.counts.new_tracks == 1
    assert result.counts.removed_tracks == 1
    assert db.get_track("spotify:track:trk_nightcall") is None  # orphan removed
    assert db.get_track("spotify:track:trk_new") is not None


def test_deleted_playlist_is_forgotten_locally(fake, client, db):
    run(client, db)
    fake.lib["playlists"] = [p for p in fake.lib["playlists"] if p["id"] != "pl_local"]
    result = run(client, db)
    assert "pl_local" not in {r["id"] for r in playlist_table(db)}
    assert summarize(db).local_tracks == 0
    assert result.counts.removed_tracks == 3


def test_server_error_on_one_playlist_is_partial_and_keeps_old_data(fake, client, db):
    run(client, db)
    fake.playlist("pl_80s")["snapshot_id"] = "snap_80s_v2"
    fake.inject(
        "/playlists/pl_80s/items", 400, body={"error": {"status": 400, "message": "Bad request"}}
    )
    result = run(client, db)
    assert result.status == "partial"
    assert result.errors[0]["playlist_id"] == "pl_80s"
    row = {r["id"]: r for r in playlist_table(db)}["pl_80s"]
    assert row["contents"] == "error"
    assert row["imported_entries"] == 4  # previous contents retained
    # error status forces a retry next time even though the snapshot is unchanged
    result = run(client, db)
    assert result.status == "ok"
    assert {r["id"]: r for r in playlist_table(db)}["pl_80s"]["contents"] == "fetched"


def test_rate_limit_aborts_without_writing(fake, client, db):
    fake.inject("/playlists/pl_synth/items", 429, {"Retry-After": "9999"})
    with pytest.raises(RateLimitedError):
        run(client, db)
    assert summarize(db).unique_tracks == 0
    assert db.last_runs(1)[0]["status"] == "failed"


def test_refuses_to_mix_accounts(fake, client, db):
    run(client, db)
    fake.lib["me"] = {"id": "someone_else"}
    with pytest.raises(AccountMismatchError):
        run(client, db)


def test_import_never_writes_to_spotify(fake, client, db):
    run(client, db)
    assert all(method == "GET" for method, _ in fake.calls)
