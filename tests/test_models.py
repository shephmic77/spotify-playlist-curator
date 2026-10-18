from fake_spotify import load_library
from playlist_curator.models import (
    SkipReason,
    parse_local_uri,
    parse_playlist_item,
    parse_playlist_summary,
    parse_saved_track,
    parse_track,
)

LIB = load_library()


def test_parse_catalog_track_keeps_identifiers_and_release_info():
    t = parse_track(LIB["tracks"]["midnight_city"]).track
    assert t is not None
    assert t.uri == "spotify:track:trk_midnight"
    assert t.spotify_id == "trk_midnight"
    assert t.isrc == "FRZID1100001"
    assert t.release_year == 2011
    assert t.release_date_precision == "day"
    assert t.artist_names == "M83"
    assert not t.is_local


def test_missing_isrc_is_none_not_invented():
    t = parse_track(LIB["tracks"]["nightcall"]).track
    assert t is not None and t.isrc is None
    assert [a.name for a in t.artists] == ["Kavinsky", "Lovefoxxx"]


def test_local_file_has_no_catalog_id_or_isrc():
    t = parse_track(LIB["tracks"]["local_remix"]).track
    assert t is not None
    assert t.is_local
    assert t.spotify_id is None
    assert t.isrc is None
    assert t.name == "Song Title: Night Remix"
    assert t.album_name == "YouTube Rips"


def test_local_file_with_empty_object_falls_back_to_uri_fields_only():
    t = parse_track(LIB["tracks"]["local_bare"]).track
    assert t is not None
    assert t.name == "untitled bootleg"
    assert t.artists == ()  # URI had no artist; nothing invented
    assert t.album_name is None
    assert t.duration_ms == 187_000
    assert t.release_year is None


def test_parse_local_uri_decodes_plus_and_percent():
    parts = parse_local_uri("spotify:local:Some+Artist:YouTube+Rips:Song+Title%3A+Night+Remix:215")
    assert parts == {
        "artist": "Some Artist",
        "album": "YouTube Rips",
        "title": "Song Title: Night Remix",
        "duration_ms": 215_000,
    }
    assert parse_local_uri("spotify:track:abc") == {}


def test_null_and_episode_entries_are_skipped_with_reason():
    assert parse_track(None).skip_reason == SkipReason.UNAVAILABLE
    assert parse_track(LIB["episode"]).skip_reason == SkipReason.EPISODE
    assert parse_track({"type": "track", "name": "x"}).skip_reason == SkipReason.NO_URI


def test_playlist_item_prefers_item_and_falls_back_to_deprecated_track():
    obj = LIB["tracks"]["africa"]
    new = parse_playlist_item(
        {"added_at": "2026-01-01T00:00:00Z", "item": obj, "added_by": {"id": "u1"}}
    )
    old = parse_playlist_item({"added_at": None, "track": obj})
    assert new.track == old.track
    assert new.added_by == "u1"
    assert new.added_at is not None and new.added_at.year == 2026
    assert old.added_at is None


def test_saved_track_parses_added_at():
    p = parse_saved_track(
        {"added_at": "2026-09-01T12:00:00Z", "track": LIB["tracks"]["midnight_city"]}
    )
    assert p.track is not None
    assert p.added_at is not None and p.added_at.month == 9


def test_playlist_summary_accepts_new_items_and_legacy_tracks_reference():
    base = {"id": "p1", "name": "n", "owner": {"id": "me"}, "snapshot_id": "s"}
    assert parse_playlist_summary({**base, "items": {"total": 7}}).total_items == 7
    assert parse_playlist_summary({**base, "tracks": {"total": 3}}).total_items == 3
    s = parse_playlist_summary(base)
    assert s.total_items is None
    assert s.owned_by("me") and not s.owned_by("other")
