from datetime import UTC, datetime

import httpx
import pytest

from fake_metadata import FakeMetadataAPIs
from playlist_curator.crate.importer import import_library
from playlist_curator.metadata.descriptors import coverage, enrich, load_descriptors
from playlist_curator.metadata.providers import (
    LastfmProvider,
    MusicBrainzProvider,
    ProviderError,
    ProviderFatal,
    RateLimiter,
    RecordingQuery,
    clean_lookup_title,
)
from playlist_curator.metadata.tags import normalize_tag, weights_from_counts


@pytest.fixture
def apis():
    return FakeMetadataAPIs()


def mb(db, apis, sleeps=None):
    http = httpx.Client(
        transport=httpx.MockTransport(apis.handle),
        headers=MusicBrainzProvider.make_http("https://example.org/me").headers,
    )
    return MusicBrainzProvider(
        db, http, sleep=(sleeps if sleeps is not None else []).append, min_interval=0
    )


def lfm(db, apis, sleeps=None):
    return LastfmProvider(
        db, apis.http(), "KEY", sleep=(sleeps if sleeps is not None else []).append, min_interval=0
    )


def imported(client, db):
    import_library(client, db, progress=lambda _m: None)
    return db


MIDNIGHT = RecordingQuery("isrc:FRZID1100001", "Midnight City", "M83", "FRZID1100001")


# --- tags --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Synth-Pop", "synthpop"),
        ("1980s", "80s"),
        ("80's", "80s"),
        ("eighties", "80s"),
        ("Hip-Hop", "hip hop"),
        ("post punk", "post-punk"),
        ("seen live", None),
        ("under 2000 listeners", None),
        ("my favourite songs", None),
        ("M83", None),  # the artist's own name
        ("female vocalists", "female vocalists"),
    ],
)
def test_normalize_tag(raw, expected):
    assert normalize_tag(raw, artist="M83") == expected


def test_weights_relative_to_strongest_and_cleaned():
    w = weights_from_counts(
        {"80s": 100, "Synth Pop": 50, "synthpop": 80, "favorites": 90, "rare": 5}
    )
    assert w == {"80s": 1.0, "synthpop": 0.8}


def test_clean_lookup_title_keeps_version_words():
    assert clean_lookup_title('"Heroes" - 2017 Remaster') == '"Heroes"'
    assert clean_lookup_title("Nightcall (feat. Lovefoxxx)") == "Nightcall"
    assert clean_lookup_title("Take On Me - Live from MTV Unplugged").endswith("Unplugged")


def test_rate_limiter_spaces_requests():
    t = [0.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    rl = RateLimiter(1.1, clock=lambda: t[0], sleep=sleep)
    rl.wait()
    t[0] += 0.3
    rl.wait()
    assert slept == [pytest.approx(0.8)]


# --- MusicBrainz ------------------------------------------------------------------


def test_musicbrainz_isrc_lookup_tags_year_and_artist(db, apis):
    p = mb(db, apis)
    out = p.lookup(MIDNIGHT)
    assert out.found and not out.from_cache
    assert out.facts["first_release_year"] == 2011
    assert out.tags["mb_recording"] == {"synthpop": 1.0, "electronic": 0.75}  # "seen live" dropped
    assert out.tags["mb_artist"]["electronic"] == 1.0
    assert (
        "PlaylistCurator/" in apis.user_agents[0]
        and "https://example.org/me" in apis.user_agents[0]
    )
    n = len(apis.calls)
    again = p.lookup(MIDNIGHT)
    assert again.from_cache and len(apis.calls) == n
    assert again.tags == out.tags


def test_musicbrainz_prefers_recording_with_matching_title(db, apis):
    out = mb(db, apis).lookup(RecordingQuery("r", "Take On Me", "a-ha", "GBAYE8500001"))
    assert "polka" not in out.tags["mb_recording"]
    assert out.facts["first_release_year"] == 1984


def test_musicbrainz_not_found_is_cached(db, apis):
    p = mb(db, apis)
    q = RecordingQuery("r", "Nope", "X", "XX0000000000")
    assert p.lookup(q).found is False
    n = len(apis.calls)
    assert p.lookup(q).found is False and len(apis.calls) == n


def test_musicbrainz_retries_503_with_retry_after(db, apis):
    sleeps: list[float] = []
    p = mb(db, apis, sleeps)
    apis.injected.append(httpx.Response(503, headers={"Retry-After": "3"}))
    assert p.lookup(MIDNIGHT).found
    assert 3.0 in sleeps


def test_no_isrc_means_no_musicbrainz_request(db, apis):
    out = mb(db, apis).lookup(RecordingQuery("r", "Local", "X", None))
    assert out.found is False and apis.calls == []


def test_offline_mode_never_hits_network(db, apis):
    p = mb(db, apis)
    assert p.lookup(MIDNIGHT, offline=True) is None
    assert apis.calls == []


# --- Last.fm -------------------------------------------------------------------------


def test_lastfm_track_and_artist_tags(db, apis):
    out = lfm(db, apis).lookup(MIDNIGHT)
    assert out.found
    assert out.tags["lastfm_track"] == {"synthwave": 1.0, "80s": 0.6, "electronic": 0.55}
    assert out.tags["lastfm_artist"]["electronic"] == 1.0
    assert all("api_key" not in c for c in apis.calls)  # key never logged in our call list


def test_lastfm_single_tag_object_quirk(db, apis):
    out = lfm(db, apis).lookup(RecordingQuery("r", "Nightcall", "Kavinsky", None))
    assert out.tags["lastfm_artist"] == {"synthwave": 1.0}


def test_lastfm_not_found_and_bad_key(db, apis):
    p = lfm(db, apis)
    assert p.lookup(RecordingQuery("r", "Unknown", "Nobody", None)).found is False
    apis.lastfm_error = 10
    with pytest.raises(ProviderError, match="API key"):
        p.lookup(RecordingQuery("r2", "Other", "Someone", None))


def test_lastfm_rate_limit_error_is_retried(db, apis):
    sleeps: list[float] = []
    p = lfm(db, apis, sleeps)
    apis.lastfm_error = 29
    assert p.lookup(MIDNIGHT).found
    assert sleeps


def test_lastfm_requires_key_for_network_use(db):
    with pytest.raises(ProviderError, match="LASTFM_API_KEY"):
        LastfmProvider(db, httpx.Client(), "")


def test_lastfm_respects_cache_control(db, apis):
    apis.cache_control = "max-age=604800"
    lfm(db, apis).lookup(MIDNIGHT)
    row = db.conn.execute(
        "SELECT fetched_at, expires_at FROM metadata_cache WHERE provider='lastfm' LIMIT 1"
    ).fetchone()
    span = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["fetched_at"])
    assert span.days == 7


# --- enrichment end to end ------------------------------------------------------------


def test_enrich_whole_crate_then_load_offline(fake, client, db, apis):
    imported(client, db)
    stats = enrich(db, [mb(db, apis), lfm(db, apis)])
    by = {s.provider: s for s in stats}
    assert by["musicbrainz"].recordings == 10
    assert by["musicbrainz"].found == 3  # Midnight City, Take On Me, Africa
    assert by["lastfm"].found >= 5
    calls = len(apis.calls)

    desc = load_descriptors(db)  # cache only
    assert len(apis.calls) == calls
    midnight = desc["isrc:FRZID1100001"].combined
    assert midnight["synthwave"] == 1.0
    assert midnight["dream pop"] == pytest.approx(0.6 * 0.6)  # artist-level, discounted
    tom = desc["isrc:GBAYE8500001"]
    assert tom.facts["first_release_year"] == 1984
    cov = coverage(db)
    assert cov["with_any_tags"] >= 6 and cov["with_first_release_year"] == 3

    again = enrich(db, [mb(db, apis), lfm(db, apis)])
    assert all(s.requests == 0 for s in again)  # fully cached second run


def test_enrich_matches_local_file_by_parsed_name(fake, client, db, apis):
    imported(client, db)
    enrich(db, [lfm(db, apis)])
    local = [rid for rid in load_descriptors(db) if "Nightcall" in rid]
    assert local, "local Kavinsky - Nightcall file should be looked up as Kavinsky / Nightcall"
    assert load_descriptors(db)[local[0]].combined.get("synthwave") == 1.0


def test_enrich_limit_and_refresh(fake, client, db, apis):
    imported(client, db)
    (s,) = enrich(db, [mb(db, apis)], limit=2)
    assert s.recordings == 2
    enrich(db, [mb(db, apis)])
    before = len(apis.calls)
    enrich(db, [mb(db, apis)], refresh=True)
    assert len(apis.calls) > before


def test_enrich_stops_cleanly_on_bad_key(fake, client, db, apis):
    imported(client, db)
    apis.lastfm_error = 26
    (s,) = enrich(db, [lfm(db, apis)])
    assert s.stopped_early and "API key" in s.stopped_early


def test_descriptor_freshness_uses_utc(db, apis):
    lfm(db, apis).lookup(MIDNIGHT)
    row = db.conn.execute("SELECT expires_at FROM metadata_cache LIMIT 1").fetchone()
    assert datetime.fromisoformat(row[0]) > datetime.now(UTC)


def test_musicbrainz_inc_is_space_separated_in_the_query(db, apis):
    mb(db, apis).lookup(MIDNIGHT)
    # httpx turns spaces into "+", which MusicBrainz reads as separators. A literal "+"
    # would become %2B and be rejected (the bug that stopped the first real run).
    assert apis.raw_queries[0].startswith("inc=artists+tags&")
    assert "%2B" not in apis.raw_queries[0]


def test_malformed_isrc_is_skipped_not_fatal(fake, client, db, apis):
    imported(client, db)
    apis.malformed_isrcs.add("FRZID1100001")
    (s,) = enrich(db, [mb(db, apis)])
    assert s.stopped_early is None
    assert s.found == 2  # Take On Me and Africa still processed
    status = db.conn.execute(
        "SELECT status FROM metadata_cache WHERE lookup_key='isrc:FRZID1100001'"
    ).fetchone()[0]
    assert status == "invalid"


def test_musicbrainz_403_stops_the_run(fake, client, db, apis):
    imported(client, db)
    apis.forbidden.update({"FRZID1100001", "GBAYE8500001", "USSM19801234"})
    (s,) = enrich(db, [mb(db, apis)])
    assert s.stopped_early and "refused" in s.stopped_early


def test_isolated_errors_are_counted_and_skipped(fake, client, db, apis):
    imported(client, db)
    apis.injected.extend([httpx.Response(500)] * 5)  # one lookup exhausts its retries
    (s,) = enrich(db, [mb(db, apis)])
    assert s.errors == 1 and s.stopped_early is None
    assert s.recordings == 9


def test_repeated_errors_stop_the_run(fake, client, db, apis, monkeypatch):
    from playlist_curator.metadata import descriptors

    monkeypatch.setattr(descriptors, "MAX_CONSECUTIVE_ERRORS", 3)  # small fixture library
    imported(client, db)
    apis.injected.extend([httpx.Response(500)] * 200)
    (s,) = enrich(db, [mb(db, apis)])
    assert s.stopped_early and "in a row" in s.stopped_early


def test_bad_lastfm_key_is_fatal(db, apis):
    apis.lastfm_error = 10
    with pytest.raises(ProviderFatal):
        lfm(db, apis).lookup(MIDNIGHT)


def test_falls_back_when_an_inc_value_is_rejected_and_remembers(fake, client, db, apis):
    imported(client, db)
    apis.isrc_inc = {"artist-credits", "tags"}  # if "artists" weren't accepted
    (s,) = enrich(db, [mb(db, apis)])
    assert s.found == 3
    isrc_queries = [q for q in apis.raw_queries if "artist" in q.split("&")[0]]
    rejected = [q for q in isrc_queries if q.startswith("inc=artists+")]
    assert len(rejected) == 1  # tried once, then the working variant is reused


def test_last_resort_is_a_lookup_without_inc(fake, client, db, apis):
    imported(client, db)
    apis.isrc_inc = set()  # every inc value rejected; plain lookup still works
    (s,) = enrich(db, [mb(db, apis)])
    assert s.found == 3 and s.stopped_early is None


def test_every_lookup_rejected_stops_early_with_the_server_message(db, apis, monkeypatch):
    p = mb(db, apis)
    monkeypatch.setattr(p, "INVALID_STREAK_LIMIT", 3)
    for i in range(3):
        apis.malformed_isrcs.add(f"XX000000000{i}")
    with pytest.raises(ProviderFatal, match="Invalid isrc"):
        for i in range(3):
            p.lookup(RecordingQuery(f"r{i}", "T", "A", f"XX000000000{i}"))


def test_previous_invalid_results_are_retried_next_run(fake, client, db, apis):
    imported(client, db)
    apis.malformed_isrcs.add("FRZID1100001")
    enrich(db, [mb(db, apis)])
    apis.malformed_isrcs.clear()
    (s,) = enrich(db, [mb(db, apis)])
    assert s.found == 3  # Midnight City was retried, not skipped for 30 days


def test_503_with_retry_after_zero_still_backs_off(db, apis):
    sleeps: list[float] = []
    p = mb(db, apis, sleeps)
    apis.injected.append(httpx.Response(503, headers={"Retry-After": "0"}))
    p.lookup(MIDNIGHT)
    assert sleeps and sleeps[0] >= 2
