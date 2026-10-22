"""Turn cached provider data into per-recording descriptors, and run enrichment.

``enrich`` talks to the network (rate-limited, resumable: every response is cached as it
arrives, so an interrupted run picks up where it stopped). ``load_descriptors`` never does;
it rebuilds descriptors from the cache, so it's cheap to call from the CLI and UI.

Combined weight of a tag for a recording:
    combined = max(song-level weight, 0.6 × artist-level weight)
Song-level tags (Last.fm track, MusicBrainz recording) describe this song; artist-level tags
describe the artist in general, so they count, but less. Every weight keeps its source so the
UI can show where it came from.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from playlist_curator.crate.deduplication import ensure_current, local_title_and_artist
from playlist_curator.metadata.providers import (
    AcousticBrainzProvider,
    LastfmProvider,
    MusicBrainzProvider,
    ProviderError,
    ProviderFatal,
    RecordingQuery,
    clean_lookup_title,
)
from playlist_curator.storage.database import Database

SONG_SOURCES = ("lastfm_track", "mb_recording", "acousticbrainz")
ARTIST_SOURCES = ("lastfm_artist", "mb_artist")
ARTIST_DISCOUNT = 0.6


@dataclass
class Descriptors:
    recording_id: str
    by_source: dict[str, dict[str, float]] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def combined(self) -> dict[str, float]:
        song: dict[str, float] = {}
        artist: dict[str, float] = {}
        for src, tags in self.by_source.items():
            target = song if src in SONG_SOURCES else artist
            for t, w in tags.items():
                target[t] = max(target.get(t, 0.0), w)
        out = dict(song)
        for t, w in artist.items():
            out[t] = max(out.get(t, 0.0), round(ARTIST_DISCOUNT * w, 4))
        return out

    @property
    def sound(self) -> dict[str, float]:
        """The song's own vibe plus its genre / era labels, judged as an individual
        (metadata/vibes.py, built-in tag classes). Use this for matching; ``combined`` (which
        mixes in artist tags) is only for describing old playlists."""
        from playlist_curator.metadata.vibes import TagClasses, profile

        return profile(self.by_source, TagClasses()).merged

    @property
    def has_song_level(self) -> bool:
        return any(self.by_source.get(s) for s in SONG_SOURCES)


def build_queries(db: Database) -> list[RecordingQuery]:
    """One query per recording, built from its primary copy."""
    ensure_current(db)
    queries = []
    for row in db.conn.execute(
        "SELECT recording_id, primary_uri FROM recordings ORDER BY recording_id"
    ):
        t = db.get_track(row["primary_uri"])
        if t is None:
            continue
        if t.is_local:
            title, artist = local_title_and_artist(t)
        else:
            title, artist = t.name, (t.artists[0].name if t.artists else None)
        queries.append(
            RecordingQuery(
                recording_id=row["recording_id"],
                title=clean_lookup_title(title) if title else "",
                artist=artist,
                isrc=None if t.is_local else t.isrc,
            )
        )
    return queries


def offline_providers(db: Database) -> list[Any]:
    """Providers that only read the cache (no HTTP client, no key needed)."""
    return [
        MusicBrainzProvider(db, None, min_interval=0),  # type: ignore[arg-type]
        LastfmProvider(db, None, api_key="", min_interval=0),  # type: ignore[arg-type]
        AcousticBrainzProvider(db, None, min_interval=0),  # type: ignore[arg-type]
    ]


def load_descriptors(db: Database) -> dict[str, Descriptors]:
    providers = offline_providers(db)
    out: dict[str, Descriptors] = {}
    for q in build_queries(db):
        d = Descriptors(q.recording_id)
        for p in providers:
            outcome = p.lookup(q, offline=True)
            if outcome is None or not outcome.found:
                continue
            d.by_source.update(outcome.tags)
            d.facts.update(outcome.facts)
        out[q.recording_id] = d
    return out


@dataclass
class EnrichStats:
    provider: str
    recordings: int = 0
    found: int = 0
    not_found: int = 0
    from_cache: int = 0
    errors: int = 0
    requests: int = 0
    stopped_early: str | None = None
    last_error: str | None = None


MAX_CONSECUTIVE_ERRORS = 10


def enrich(
    db: Database,
    providers: list[Any],
    *,
    limit: int | None = None,
    refresh: bool = False,
    progress: Callable[[str], None] | None = None,
    only: set[str] | None = None,
    retry_invalid: bool = True,
) -> list[EnrichStats]:
    """Fetch tags. ``only`` limits it to these recording ids (maintenance: new songs only);
    ``retry_invalid=False`` keeps earlier "invalid identifier" results instead of retrying."""
    say = progress or (lambda _m: None)
    queries = build_queries(db)
    if only is not None:
        queries = [q for q in queries if q.recording_id in only]
    if limit is not None:
        queries = queries[:limit]
    results = []
    for p in providers:
        if refresh:
            db.conn.execute("DELETE FROM metadata_cache WHERE provider=?", (p.name,))
        if retry_invalid and hasattr(p, "prepare_run"):
            p.prepare_run()
        if hasattr(p, "prefetch"):
            try:
                p.prefetch(queries, say)
            except ProviderError as exc:  # fall back to one-by-one lookups
                say(f"{p.name}: bulk fetch failed ({exc}); continuing one by one")
        stats = EnrichStats(p.name)
        todo = len(queries)
        say(f"{p.name}: {todo} recordings")
        streak = 0
        for i, q in enumerate(queries, 1):
            try:
                outcome = p.lookup(q)
            except ProviderFatal as exc:
                stats.stopped_early = str(exc)
                say(f"{p.name}: stopped: {exc}")
                break
            except ProviderError as exc:
                # One bad lookup: skip this recording, keep going. Many in a row usually
                # means the service is down, so stop instead of hammering it.
                stats.errors += 1
                stats.last_error = str(exc)
                streak += 1
                if streak >= MAX_CONSECUTIVE_ERRORS:
                    stats.stopped_early = f"{streak} failures in a row; last: {exc}"
                    say(f"{p.name}: stopped: {stats.stopped_early}")
                    break
                continue
            streak = 0
            stats.recordings += 1
            if outcome is not None and outcome.found:
                stats.found += 1
            else:
                stats.not_found += 1
            if outcome is not None and outcome.from_cache:
                stats.from_cache += 1
            if i % 50 == 0 or i == todo:
                say(f"  {p.name}: {i}/{todo}  (found {stats.found}, {p.requests} requests)")
        stats.requests = p.requests
        results.append(stats)
    return results


def coverage(db: Database) -> dict[str, int]:
    """How many recordings have any descriptors, by kind. For the inventory and UI."""
    desc = load_descriptors(db)
    return {
        "recordings": len(desc),
        "with_any_tags": sum(1 for d in desc.values() if d.by_source),
        "with_song_level_tags": sum(1 for d in desc.values() if d.has_song_level),
        "with_first_release_year": sum(1 for d in desc.values() if "first_release_year" in d.facts),
        "cache_entries": db.conn.execute("SELECT COUNT(*) FROM metadata_cache").fetchone()[0],
    }


def descriptor_json(d: Descriptors) -> str:
    return json.dumps({"sources": d.by_source, "facts": d.facts}, indent=2)
