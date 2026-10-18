"""Build synthetic libraries directly in the database (no Spotify fake needed)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from playlist_curator.crate import deduplication as dd
from playlist_curator.metadata.descriptors import Descriptors
from playlist_curator.models import ArtistRef, PlaylistSummary, SourceEntry, SourceKind, Track

START = datetime(2024, 1, 1, tzinfo=UTC)


def add_playlist(db, pid, name, songs, *, owned=True, liked=()):
    """songs: list of (uri, title, artist, year)."""
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
                added_at=START + timedelta(days=i),
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
    if liked:
        liked_entries = [
            SourceEntry(track_uri=u, kind=SourceKind.LIKED, source_id="__liked__", position=i)
            for i, u in enumerate(liked)
        ]
        db.replace_source_entries(SourceKind.LIKED, "__liked__", liked_entries)
    dd.rebuild(db)


def descriptors_for(db, tags_by_uri):
    out = {}
    for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings"):
        d = Descriptors(r["recording_id"])
        if r["primary_uri"] in tags_by_uri:
            d.by_source["lastfm_track"] = tags_by_uri[r["primary_uri"]]
        out[r["recording_id"]] = d
    return out


GROUPS = {
    "chill": {"indie": 1.0, "chill": 0.8, "dream pop": 0.5},
    "gym": {"rock": 1.0, "hard rock": 0.8, "energetic": 0.6},
    "jazz": {"jazz": 1.0, "smooth jazz": 0.7, "saxophone": 0.5},
    "synth": {"80s": 1.0, "synthpop": 0.8, "new wave": 0.6},
    "classic": {"classic rock": 1.0, "70s": 0.7, "soft rock": 0.6},
    "rap": {"hip hop": 1.0, "rap": 0.9, "trap": 0.4},
}
YEARS = {"chill": 2015, "gym": 2005, "jazz": 1960, "synth": 1984, "classic": 1976, "rap": 2016}


def song(group, i):
    return (
        f"spotify:track:{group}{i}",
        f"{group.title()} Song {i}",
        f"{group} artist {i % 5}",
        YEARS[group] + i % 6,
    )


def song_tags(group, i, rng):
    return {t: round(w * rng.uniform(0.7, 1.0), 3) for t, w in GROUPS[group].items()}


def build_library(db):
    """Six sound groups of 20 songs, spread over old playlists like a real messy library."""
    rng = random.Random(7)
    tags = {}
    for g in GROUPS:
        for i in range(20):
            tags[f"spotify:track:{g}{i}"] = song_tags(g, i, rng)
    # odd ones out
    tags["spotify:track:rs0"] = {"soundtrack": 1.0}
    chill = [song("chill", i) for i in range(20)]
    add_playlist(db, "pl_u", "U", chill[:12])
    add_playlist(db, "pl_enye", "Ñ", chill[8:])
    add_playlist(db, "pl_w", "W", [song("gym", i) for i in range(20)] + [song("jazz", 0)])
    add_playlist(db, "pl_80s", "80s", [song("synth", i) for i in range(20)])
    add_playlist(db, "pl_n", "N", [song("classic", i) for i in range(20)])
    add_playlist(db, "pl_r", "R", [song("rap", i) for i in range(20)])
    add_playlist(db, "pl_m", "M", [song("jazz", i) for i in range(20)] + chill[:5])
    add_playlist(
        db,
        "pl_rs",
        "RS",
        [("spotify:track:rs0", "Theme Song", "Show Band", 2010), song("synth", 3)],
    )
    return descriptors_for(db, tags)
