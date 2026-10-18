import sqlite3

import pytest

from playlist_curator.crate import deduplication as dd
from playlist_curator.crate.importer import import_library
from playlist_curator.crate.inventory import (
    duplicate_groups,
    local_files_table,
    pending_suggestions,
    summarize,
)
from playlist_curator.models import ArtistRef, Track
from playlist_curator.storage.database import MIGRATIONS, Database

TOM_ALBUM = "spotify:track:trk_tom_album"
TOM_COMP = "spotify:track:trk_tom_comp"
TOM_LIVE = "spotify:track:trk_tom_mtv"
AFRICA = "spotify:track:trk_africa"
AFRICA_RM = "spotify:track:trk_africa_rm"
NIGHTCALL = "spotify:track:trk_nightcall"
LOCAL_NIGHTCALL = "spotify:local:::Kavinsky+-+Nightcall+%28Official+Video%29:261"


def run(client, db):
    return import_library(client, db, progress=lambda _m: None)


def rec_of(db, uri):
    row = db.conn.execute(
        "SELECT recording_id FROM recording_members WHERE uri=?", (uri,)
    ).fetchone()
    return row[0] if row else None


def track(uri, name, artist, *, isrc=None, ms=200_000, local=False, album_type="album"):
    return Track(
        uri=uri,
        name=name,
        artists=(ArtistRef(name=artist),) if artist else (),
        isrc=isrc,
        duration_ms=ms,
        is_local=local,
        album_type=None if local else album_type,
    )


# --- title parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "base", "versions"),
    [
        ("Africa", "africa", set()),
        ("Africa - Remastered 2011", "africa", set()),
        ("Take On Me - Live from MTV Unplugged", "take on me", {"live"}),
        ("Live and Let Die", "live and let die", set()),
        ("(Don't Fear) The Reaper", "dont fear the reaper", set()),
        ("Blue Monday - 1988 Mix", "blue monday", set()),
        ("Strobe - Radio Edit", "strobe", {"edit"}),
        ("Nightcall (feat. Lovefoxxx)", "nightcall", set()),
        ("Love Story (Taylor's Version)", "love story", {"re-recorded"}),
        ("Song (with X) - Remix", "song", {"remix"}),
        ("Fade Into You - sped up", "fade into you", {"speed"}),
        ("Hurt (Official Video)", "hurt", set()),
        ("Café Del Mar", "cafe del mar", set()),
        ("Rock & Roll", "rock and roll", set()),
    ],
)
def test_parse_title(title, base, versions):
    parts = dd.parse_title(title)
    assert parts.base == base
    assert parts.versions == frozenset(versions)


def test_local_title_split_only_when_artist_missing():
    t = track("spotify:local:x", "Kavinsky - Nightcall (Official Video)", None, local=True)
    assert dd.local_title_and_artist(t) == ("Nightcall (Official Video)", "Kavinsky")
    tagged = track("spotify:local:y", "A - B", "Real Artist", local=True)
    assert dd.local_title_and_artist(tagged) == ("A - B", "Real Artist")
    remaster = track("spotify:local:z", "Song - Remastered 2011", None, local=True)
    assert dd.local_title_and_artist(remaster) == ("Song - Remastered 2011", None)


# --- grouping and suggestions (pure) ------------------------------------------------


def test_versions_and_different_artists_are_not_suggested():
    tracks = [
        track("spotify:track:a", "Strobe", "deadmau5", isrc="A1"),
        track("spotify:track:b", "Strobe - Radio Edit", "deadmau5", isrc="A2", ms=210_000),
        track("spotify:track:c", "Strobe", "Someone Else", isrc="A3"),
    ]
    recs = dd.group_recordings(tracks, {})
    assert len(recs) == 3
    assert dd.find_suggestions(recs, tracks, {}) == []


def test_title_match_needs_similar_length():
    tracks = [
        track("spotify:track:a", "Song", "X", isrc="A1", ms=200_000),
        track("spotify:track:b", "Song - 2015 Remaster", "X", isrc="A2", ms=260_000),
    ]
    recs = dd.group_recordings(tracks, {})
    assert dd.find_suggestions(recs, tracks, {}) == []


def test_primary_prefers_most_used_then_album_over_compilation():
    tracks = [
        track("spotify:track:comp", "S", "X", isrc="I", album_type="compilation"),
        track("spotify:track:alb", "S", "X", isrc="I", album_type="album"),
    ]
    (rec,) = dd.group_recordings(tracks, {})
    assert rec.primary_uri == "spotify:track:alb"
    (rec,) = dd.group_recordings(tracks, {}, {"spotify:track:comp": 5, "spotify:track:alb": 1})
    assert rec.primary_uri == "spotify:track:comp"
    assert rec.recording_id == "isrc:I"


def test_local_file_without_artist_is_only_a_possible_match():
    tracks = [
        track("spotify:track:a", "Hurt", "Johnny Cash", isrc="A1", ms=218_000),
        track("spotify:local:::Hurt:218", "Hurt", None, local=True, ms=218_000),
    ]
    recs = dd.group_recordings(tracks, {})
    (s,) = dd.find_suggestions(recs, tracks, {})
    assert s.kind == "local_match"
    assert s.strength == "possible"
    assert "no artist tag" in s.reason


def test_local_file_by_other_artist_is_not_suggested():
    tracks = [
        track("spotify:track:a", "Hurt", "Johnny Cash", isrc="A1"),
        track("spotify:local:Nine+Inch+Nails::Hurt:200", "Hurt", "Nine Inch Nails", local=True),
    ]
    recs = dd.group_recordings(tracks, {})
    assert dd.find_suggestions(recs, tracks, {}) == []


# --- end to end with the fake library ---------------------------------------------


def test_same_isrc_is_merged_but_live_version_is_not(fake, client, db):
    run(client, db)
    assert rec_of(db, TOM_ALBUM) == rec_of(db, TOM_COMP) == "isrc:GBAYE8500001"
    assert rec_of(db, TOM_LIVE) != rec_of(db, TOM_ALBUM)
    (group,) = duplicate_groups(db)
    assert group["title"] == "Take On Me"
    primary = next(m for m in group["members"] if m["primary"])
    assert primary["uri"] == TOM_ALBUM  # album beats compilation
    assert {m["evidence"] for m in group["members"]} == {"isrc"}


def test_recording_level_inventory(fake, client, db):
    run(client, db)
    s = summarize(db)
    assert s.unique_tracks == 11
    assert s.unique_recordings == 10
    assert s.merged_groups == 1 and s.duplicate_uris_merged == 1
    # Take On Me: album copy is liked, compilation copy is in a playlist -> counts as both
    assert s.rec_in_both == 2  # Midnight City, Take On Me
    assert s.pending_title_suggestions == 1
    assert s.pending_local_suggestions == 1
    assert s.local_matched == 0 and s.local_unmatched == 3


def test_accepting_remaster_suggestion_merges_and_persists(fake, client, db):
    run(client, db)
    (sugg,) = pending_suggestions(db, "title_match")
    assert {sugg["a"]["uri"], sugg["b"]["uri"]} == {AFRICA, AFRICA_RM}
    assert "Remastered 2011" in sugg["reason"]

    a, b = dd.resolve_suggestion(db, sugg["id"][:5])
    dd.decide(db, a, b, "same")
    assert rec_of(db, AFRICA) == rec_of(db, AFRICA_RM)
    assert pending_suggestions(db, "title_match") == []

    run(client, db)  # rebuild after re-import keeps the decision
    assert rec_of(db, AFRICA) == rec_of(db, AFRICA_RM)
    group = next(g for g in duplicate_groups(db) if g["title"].startswith("Africa"))
    assert {m["evidence"] for m in group["members"]} == {"accepted"}


def test_rejecting_a_suggestion_is_remembered(fake, client, db):
    run(client, db)
    (sugg,) = pending_suggestions(db, "title_match")
    dd.decide(db, sugg["a"]["uri"], sugg["b"]["uri"], "different")
    run(client, db)
    assert pending_suggestions(db, "title_match") == []
    assert rec_of(db, AFRICA) != rec_of(db, AFRICA_RM)


def test_local_match_accept_links_local_file_to_recording(fake, client, db):
    run(client, db)
    (sugg,) = pending_suggestions(db, "local_match")
    assert sugg["strength"] == "strong"
    assert sugg["a"]["uri"] == LOCAL_NIGHTCALL and sugg["b"]["uri"] == NIGHTCALL
    dd.decide(db, LOCAL_NIGHTCALL, NIGHTCALL, "same")

    assert rec_of(db, LOCAL_NIGHTCALL) == rec_of(db, NIGHTCALL)
    row = next(r for r in local_files_table(db) if r["uri"] == LOCAL_NIGHTCALL)
    assert row["matched_recording"].startswith("Nightcall")
    assert row["title"] == "Nightcall (Official Video)" and row["artist"] == "Kavinsky"
    # The catalog copy stays the one used for writing playlists
    primary = db.conn.execute(
        "SELECT primary_uri FROM recordings WHERE recording_id=?", (rec_of(db, NIGHTCALL),)
    ).fetchone()[0]
    assert primary == NIGHTCALL
    s = summarize(db)
    assert s.local_matched == 1 and s.local_unmatched == 2


def test_split_overrides_isrc_and_undo_restores(fake, client, db):
    run(client, db)
    dd.split_out(db, TOM_COMP)
    assert rec_of(db, TOM_COMP) != rec_of(db, TOM_ALBUM)
    assert duplicate_groups(db) == []
    # a split pair is decided, so it isn't re-suggested either
    assert all(TOM_COMP not in (s["a"]["uri"], s["b"]["uri"]) for s in pending_suggestions(db))

    key = dd.pair_key(TOM_ALBUM, TOM_COMP)
    dd.undo_decision(db, key)
    assert rec_of(db, TOM_COMP) == rec_of(db, TOM_ALBUM)


def test_decisions_survive_a_track_leaving_and_returning(fake, client, db):
    run(client, db)
    dd.decide(db, AFRICA, AFRICA_RM, "same")
    synth = fake.playlist("pl_synth")
    synth["entries"].remove("africa_remaster")
    synth["snapshot_id"] = "v2"
    run(client, db)
    assert db.get_track(AFRICA_RM) is None
    synth["entries"].append("africa_remaster")
    synth["snapshot_id"] = "v3"
    run(client, db)
    assert rec_of(db, AFRICA) == rec_of(db, AFRICA_RM)


def test_decide_validates_input(db):
    with pytest.raises(ValueError):
        dd.decide(db, "a", "b", "maybe")
    with pytest.raises(ValueError):
        dd.decide(db, "a", "a", "same")
    with pytest.raises(KeyError):
        dd.resolve_suggestion(db, "deadbeef")


def test_v1_database_upgrades_and_backfills_recordings(tmp_path, fake, client):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0])
    conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    conn.commit()
    conn.close()

    with Database(path) as db:
        assert db.schema_version() == len(MIGRATIONS)
        run(client, db)
        db.conn.execute("DELETE FROM recording_members")  # simulate stale derived data
        dd.ensure_current(db)
        assert summarize(db).unique_recordings == 10


def test_dated_version_label_warns_about_possible_rerecording():
    tracks = [
        track("spotify:track:a", "Crazy Train", "Ozzy Osbourne", isrc="A1", ms=293_000),
        track(
            "spotify:track:b", "Crazy Train - 2002 Version", "Ozzy Osbourne", isrc="A2", ms=289_000
        ),
    ]
    recs = dd.group_recordings(tracks, {})
    (s,) = dd.find_suggestions(recs, tracks, {})
    assert "re-recorded" in s.reason
