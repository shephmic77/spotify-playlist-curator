import json

import pytest

from playlist_curator.crate.importer import import_library
from playlist_curator.curator import feedback as fb
from playlist_curator.curator import identities as idn
from playlist_curator.curator import scoring as sc
from playlist_curator.metadata.descriptors import Descriptors
from playlist_curator.spotify import sync
from playlist_curator.storage.database import utcnow

SYNTH = {"80s": 1.0, "synthwave": 0.9}
TAGS = {
    "spotify:track:trk_midnight": SYNTH,
    "spotify:track:trk_nightcall": SYNTH,
    "spotify:track:trk_tom_album": {"80s": 1.0, "synthpop": 0.8},
    "spotify:track:trk_africa": {"80s": 0.9, "soft rock": 0.8},
    "spotify:track:trk_clair": {"classical": 1.0, "piano": 0.9},
    "spotify:local:Some+Artist:YouTube+Rips:Song+Title%3A+Night+Remix:215": SYNTH,
}


def descriptors(db):
    out = {}
    for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings"):
        d = Descriptors(r["recording_id"])
        if r["primary_uri"] in TAGS:
            d.by_source["lastfm_track"] = TAGS[r["primary_uri"]]
        out[r["recording_id"]] = d
    return out


@pytest.fixture
def setup(fake, client, db):
    import_library(client, db, progress=lambda _m: None)
    sc.set_coverage(db, False)  # these tests are about sync mechanics, not leftover placement
    desc = descriptors(db)
    night = idn.create_identity(db, "Night Drive", concept="synth at night")
    idn.edit_identity(db, night.identity_id, add_tags={"synthwave": 1.0, "80s": 0.6})
    now = utcnow()
    db.conn.executemany(
        "INSERT INTO identity_examples VALUES (?,?,'reference','user',?)",
        [
            (night.identity_id, u, now)
            for u in ("spotify:track:trk_midnight", "spotify:track:trk_nightcall")
        ],
    )
    sc.build_drafts(db, descriptors=desc)
    return desc


def by_name(p):
    return {x.name: x for x in p.playlists}


def test_plan_never_writes_and_lists_local_files(fake, client, db, setup):
    p = sync.plan(db, client)
    night = by_name(p)["Night Drive"]
    assert night.action == "create" and night.spotify_id is None
    assert "spotify:track:trk_midnight" in night.add
    assert not any(u.startswith("spotify:local:") for u in night.add)
    assert night.local_files and "Night Remix" in night.local_files[0]
    assert fake.write_bodies == []
    assert all(m == "GET" for m, _ in fake.calls)


def test_apply_creates_private_playlist_and_records_it(fake, client, db, setup):
    res = sync.apply(db, client, sync.plan(db, client))
    assert res.status == "ok" and res.applied[0]["action"] == "create"
    create = next(b for m, p, b in fake.write_bodies if p == "/me/playlists")
    assert create["public"] is False and create["name"] == "Night Drive"
    assert "Made with Playlist Curator" in create["description"]
    sid = res.applied[0]["spotify_id"]
    entry = sync.managed(db)[idn.get_identity(db, "Night Drive").identity_id]
    assert entry["spotify_id"] == sid
    assert set(json.loads(entry["last_uris_json"])) == set(fake.playlist(sid)["entries"])
    assert [s["reason"] for s in sync.snapshots(db, entry["identity_id"])] == [
        "after_sync",
        "before_sync",
    ]
    # a second plan finds nothing to do
    assert by_name(sync.plan(db, client))["Night Drive"].action == "unchanged"


def test_never_writes_to_playlists_it_did_not_create(fake, client, db, setup):
    with pytest.raises(sync.NotManaged):
        sync._guard(db, "pl_80s")
    iid = idn.get_identity(db, "Night Drive").identity_id
    with pytest.raises(sync.NotManaged):
        sync.rollback(db, client, iid)
    before = len(fake.write_bodies)
    sync.apply(db, client, sync.plan(db, client))
    touched = {p for m, p, _ in fake.write_bodies[before:]}
    assert not any("pl_80s" in p or "pl_synth" in p or "pl_local" in p for p in touched)


def test_minimal_update_and_no_duplicates(fake, client, db, setup):
    res = sync.apply(db, client, sync.plan(db, client))
    sid = res.applied[0]["spotify_id"]
    iid = idn.get_identity(db, "Night Drive").identity_id
    # you decide Nightcall doesn't belong after all; add Africa as essential
    fb.decide_uri(db, idn.get_identity(db, iid), "spotify:track:trk_nightcall", "reject")
    fb.decide_uri(db, idn.get_identity(db, iid), "spotify:track:trk_africa", "essential")
    sc.build_drafts(db, descriptors=setup)
    p = by_name(sync.plan(db, client))["Night Drive"]
    assert p.action == "update"
    assert p.remove == ["spotify:track:trk_nightcall"]
    assert p.add == ["spotify:track:trk_africa"]
    n_writes = len(fake.write_bodies)
    sync.apply(db, client, sync.plan(db, client))
    entries = fake.playlist(sid)["entries"]
    assert "spotify:track:trk_nightcall" not in entries and "spotify:track:trk_africa" in entries
    assert len(entries) == len(set(entries))
    # only one DELETE and one POST of items; no full replace
    methods = [m for m, p, _ in fake.write_bodies[n_writes:] if p.endswith("/items")]
    assert sorted(methods) == ["DELETE", "POST"]


def test_same_recording_is_not_added_twice(fake, client, db, setup):
    res = sync.apply(db, client, sync.plan(db, client))
    sid = res.applied[0]["spotify_id"]
    iid = idn.get_identity(db, "Night Drive").identity_id
    fb.decide_uri(db, idn.get_identity(db, iid), "spotify:track:trk_tom_album", "essential")
    sc.build_drafts(db, descriptors=setup)
    # the compilation copy of Take On Me is already in the playlist (you added it yourself)
    fake.playlist(sid)["entries"].append("spotify:track:trk_tom_comp")
    p = by_name(sync.plan(db, client))["Night Drive"]
    assert "spotify:track:trk_tom_album" not in p.add


def test_your_spotify_edits_win(fake, client, db, setup):
    res = sync.apply(db, client, sync.plan(db, client))
    sid = res.applied[0]["spotify_id"]
    iid = idn.get_identity(db, "Night Drive").identity_id
    entries = fake.playlist(sid)["entries"]
    entries.remove("spotify:track:trk_nightcall")  # you removed it in Spotify
    entries.append("spotify:track:trk_clair")  # you added Clair de lune in Spotify
    p = by_name(sync.plan(db, client))["Night Drive"]
    assert p.keep_manual_adds == ["spotify:track:trk_clair"]
    assert p.respect_manual_removes == ["spotify:track:trk_nightcall"]
    assert "spotify:track:trk_nightcall" not in p.add
    assert "spotify:track:trk_clair" not in p.remove
    sync.apply(db, client, sync.plan(db, client))
    decisions = {d["uri"]: d["decision"] for d in fb.decisions_for(db, iid)}
    assert decisions["spotify:track:trk_clair"] == "keep"
    assert decisions["spotify:track:trk_nightcall"] == "reject"
    entries = fake.playlist(sid)["entries"]
    assert "spotify:track:trk_clair" in entries and "spotify:track:trk_nightcall" not in entries


def test_rollback_restores_previous_state(fake, client, db, setup):
    res = sync.apply(db, client, sync.plan(db, client))
    sid = res.applied[0]["spotify_id"]
    iid = idn.get_identity(db, "Night Drive").identity_id
    first = list(fake.playlist(sid)["entries"])
    fb.decide_uri(db, idn.get_identity(db, iid), "spotify:track:trk_africa", "essential")
    sc.build_drafts(db, descriptors=setup)
    sync.apply(db, client, sync.plan(db, client))
    assert "spotify:track:trk_africa" in fake.playlist(sid)["entries"]
    out = sync.rollback(db, client, iid)
    assert fake.playlist(sid)["entries"] == first
    assert out["songs"] == len(first)
    # rollback to the very first (empty) state
    empty = min(s["snapshot_id"] for s in sync.snapshots(db, iid))
    sync.rollback(db, client, iid, empty)
    assert fake.playlist(sid)["entries"] == []


def test_deleted_in_spotify_is_recreated(fake, client, db, setup):
    res = sync.apply(db, client, sync.plan(db, client))
    sid = res.applied[0]["spotify_id"]
    fake.lib["playlists"] = [p for p in fake.lib["playlists"] if p["id"] != sid]
    p = by_name(sync.plan(db, client))["Night Drive"]
    assert p.action == "create" and "deleted" in p.note
    res2 = sync.apply(db, client, sync.plan(db, client))
    assert res2.applied[0]["spotify_id"] != sid


def test_import_skips_playlists_the_app_created(fake, client, db, setup):
    res = sync.apply(db, client, sync.plan(db, client))
    sid = res.applied[0]["spotify_id"]
    import_library(client, db, progress=lambda _m: None)
    assert db.get_playlist_row(sid) is None


def test_write_errors_are_reported_per_playlist(fake, client, db, setup):
    fake.inject("/me/playlists", 400, body={"error": {"status": 400, "message": "nope"}})
    res = sync.apply(db, client, sync.plan(db, client))
    assert res.status == "failed" and "nope" in res.errors[0]["error"]
    assert sync.managed(db) == {}


def test_handpicked_and_empty_playlists(fake, client, db, setup):
    hp = idn.create_identity(db, "Mine", kind="handpicked", auto_maintain=False)
    db.conn.execute(
        "INSERT INTO identity_examples VALUES (?,?,'reference','user',?)",
        (hp.identity_id, "spotify:track:trk_clair", utcnow()),
    )
    idn.create_identity(db, "Empty")
    sc.build_drafts(db, descriptors=setup)
    p = by_name(sync.plan(db, client))
    assert p["Mine"].add == ["spotify:track:trk_clair"]
    assert p["Empty"].action == "skip" and "empty" in p["Empty"].note
    assert sync.plan(db, client, only=["mine"]).playlists[0].name == "Mine"
    assert sync.local_file_actions(db)[0][0] == "Night Drive"
