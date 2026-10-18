from datetime import UTC, datetime, timedelta

import pytest

from fake_metadata import FakeMetadataAPIs
from playlist_curator.crate import deduplication as dd
from playlist_curator.crate.importer import import_library
from playlist_curator.curator.analysis import analyze, centroid, cosine
from playlist_curator.metadata.descriptors import Descriptors, enrich
from playlist_curator.metadata.providers import LastfmProvider, MusicBrainzProvider
from playlist_curator.models import (
    ArtistRef,
    PlaylistSummary,
    SourceEntry,
    SourceKind,
    Track,
)


def test_cosine_and_centroid():
    assert cosine({"a": 1}, {"a": 2}) == pytest.approx(1.0)
    assert cosine({"a": 1}, {"b": 1}) == 0.0
    assert cosine({}, {"a": 1}) == 0.0
    assert centroid([{"a": 1.0}, {"a": 0.0, "b": 1.0}]) == {"a": 0.5, "b": 0.5}


def add_playlist(db, pid, name, songs, *, start, step_days=10, owned=True):
    """songs: list of (uri, title, artist, year). Adds tracks + entries, one every step_days."""
    tracks, entries = [], []
    for i, (uri, title, artist, year) in enumerate(songs):
        tracks.append(
            Track(
                uri=uri,
                spotify_id=uri.split(":")[-1],
                name=title,
                artists=(ArtistRef(name=artist),),
                release_date=str(year),
                release_date_precision="year",
                duration_ms=200_000,
                album_type="album",
            )
        )
        entries.append(
            SourceEntry(
                track_uri=uri,
                kind=SourceKind.PLAYLIST,
                source_id=pid,
                position=i,
                added_at=start + timedelta(days=i * step_days),
            )
        )
    with db.transaction():
        db.upsert_tracks(tracks, seen_at="2026-01-01")
        db.replace_source_entries(SourceKind.PLAYLIST, pid, entries)
        db.upsert_playlist(
            PlaylistSummary(spotify_id=pid, name=name, owner_id="me", snapshot_id="s"),
            owned_by_user=owned,
            contents_status="fetched",
            contents_note=None,
            fetched_snapshot_id="s",
            last_fetched_at="2026-01-01",
        )
    dd.rebuild(db)


def desc_for(db, tags_by_uri):
    out = {}
    for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings"):
        d = Descriptors(r["recording_id"])
        if r["primary_uri"] in tags_by_uri:
            d.by_source["lastfm_track"] = tags_by_uri[r["primary_uri"]]
        out[r["recording_id"]] = d
    return out


def test_drift_outliers_staleness_and_profile(db):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    night = [
        (f"spotify:track:n{i}", f"Night {i}", f"Synth Artist {i % 4}", 1985 + i % 5)
        for i in range(24)
    ]
    add_playlist(db, "pl_night", "Night Drive", night, start=old)
    focus = [(f"spotify:track:f{i}", f"Etude {i}", "Pianist", 1900 + i) for i in range(10)]
    focus.append(("spotify:track:metal", "Loud Song", "Metal Band", 2005))
    add_playlist(db, "pl_focus", "Focus", focus, start=old, step_days=1)

    tags = {}
    for i in range(24):  # first half synthwave, second half country -> drift
        tags[f"spotify:track:n{i}"] = (
            {"synthwave": 1.0, "80s": 0.8} if i < 12 else {"country": 1.0, "acoustic": 0.6}
        )
    for i in range(10):
        tags[f"spotify:track:f{i}"] = {"classical": 1.0, "piano": 0.9}
    tags["spotify:track:metal"] = {"metal": 1.0}

    report = analyze(db, descriptors=desc_for(db, tags))
    by = {p.name: p for p in report.playlists}

    nd = by["Night Drive"]
    assert nd.drift_similarity == 0.0
    assert any("drifted" in f for f in nd.flags)
    assert nd.year_median and 1985 <= nd.year_median <= 1989
    assert nd.decades[0][0] == "80s"

    fo = by["Focus"]
    assert [o.title for o in fo.outliers] == ["Loud Song"]
    assert fo.top_tags[0]["tag"] in ("classical", "piano")
    assert fo.top_tags[0]["support"] == pytest.approx(10 / 11, abs=0.01)
    assert fo.cohesion > 0.8
    assert any("no songs added" in f for f in fo.flags)  # last add in 2020
    assert any("misfit" in f for f in fo.flags)
    assert fo.top_artists[0] == ("Pianist", pytest.approx(10 / 11, abs=0.01))


def test_untagged_songs_are_reported_not_guessed(db):
    start = datetime.now(UTC) - timedelta(days=5)
    add_playlist(
        db,
        "p",
        "Mystery",
        [(f"spotify:track:m{i}", f"M{i}", "A", 2000) for i in range(3)],
        start=start,
        step_days=1,
    )
    (rep,) = analyze(db, descriptors=desc_for(db, {})).playlists
    assert rep.tagged == 0
    assert rep.cohesion is None and rep.top_tags == [] and rep.outliers == []
    assert any("too few tagged" in f for f in rep.flags)
    assert any("small" in f for f in rep.flags)
    assert not any("no songs added" in f for f in rep.flags)


def test_overlap_and_redundancy_with_fixture_library(fake, client, db):
    import_library(client, db, progress=lambda _m: None)
    apis = FakeMetadataAPIs()
    enrich(
        db,
        [
            MusicBrainzProvider(db, apis.http(), min_interval=0),
            LastfmProvider(db, apis.http(), "KEY", min_interval=0),
        ],
    )
    report = analyze(db)
    names = {p.name for p in report.playlists}
    assert "Today's Top Hits" not in names  # unreadable playlists aren't analyzed
    pair = next(x for x in report.pairs if {x.a, x.b} == {"80s", "Synth, Neon & Night Drives"})
    # Midnight City, Nightcall and Take On Me (album + compilation copies = one recording)
    assert pair.shared_songs == 3
    assert pair.containment == pytest.approx(0.75)
    assert pair.flag == "one is largely inside the other"
    eighties = next(p for p in report.playlists if p.name == "80s")
    assert any("Synth, Neon" in f for f in eighties.flags)
    # Africa's original year comes from MusicBrainz (1982), Take On Me's from MB (1984)
    assert eighties.year_p10 is not None and eighties.year_p10 <= 1984
    assert report.crate_tagged >= 6
    assert '"playlists"' in report.to_json()
