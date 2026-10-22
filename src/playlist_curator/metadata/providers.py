"""Independent descriptor providers: MusicBrainz and Last.fm.

The project owner chose option B (docs/data_sources.md): for each recording we send its ISRC
(MusicBrainz) or its artist + title (Last.fm), one lookup at a time, and store the tags those
services publish. Nothing else from Spotify is sent anywhere.

Every response is trimmed to what scoring needs and cached in ``metadata_cache``. Descriptors
are rebuilt from that cache on demand, so regrouping recordings never triggers new requests.

Provider terms honoured here:
- MusicBrainz: ~1 request/second per client, and a User-Agent naming the app plus a contact.
- Last.fm: non-commercial use, an API key, attribution in the UI, respect for their limits,
  and a 100 MB cap on stored data (we store tag names and counts only).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from playlist_curator import __version__
from playlist_curator.crate.deduplication import classify_qualifier, normalize_artist, parse_title
from playlist_curator.metadata.tags import weights_from_counts
from playlist_curator.storage.database import Database

log = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
AB_BASE = "https://acousticbrainz.org/api/v1"


class ProviderError(RuntimeError):
    """One lookup failed. The enrichment run skips that recording and carries on."""


class ProviderFatal(ProviderError):
    """The provider can't be used at all right now (bad key, blocked). Stop the run."""


@dataclass(frozen=True)
class RecordingQuery:
    recording_id: str
    title: str  # display title with edition noise removed, for name-based lookups
    artist: str | None
    isrc: str | None


@dataclass
class ProviderOutcome:
    """What one provider knows about one recording. ``tags`` maps source -> {tag: weight}."""

    found: bool
    tags: dict[str, dict[str, float]] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    from_cache: bool = True


def clean_lookup_title(title: str) -> str:
    """Remove edition, feat. and noise qualifiers but keep version words that change the song.

    "Heroes - 2017 Remaster" -> "Heroes"; "Nightcall (Official Video)" -> "Nightcall";
    "Take On Me - Live" stays as it is, because a live version is a different recording.
    """
    if parse_title(title).versions:
        return title

    def drop(m: re.Match[str]) -> str:
        return (
            "" if classify_qualifier(m.group(1))[0] in ("feat", "noise", "edition") else m.group(0)
        )

    cleaned = re.sub(r"\s*[\(\[]([^\)\]]*)[\)\]]", drop, title)
    dash = re.search(r"\s+[-–—]\s+(.+)$", cleaned)
    if dash and classify_qualifier(dash.group(1))[0] in ("feat", "noise", "edition"):
        cleaned = cleaned[: dash.start()]
    return cleaned.strip() or title


class RateLimiter:
    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self.clock = clock
        self.sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._last is not None:
            gap = self.min_interval - (now - self._last)
            if gap > 0:
                self.sleep(gap)
                now = self.clock()
        self._last = now


class Cache:
    def __init__(self, db: Database, provider: str) -> None:
        self.db = db
        self.provider = provider

    def get(self, key: str, *, allow_expired: bool = False) -> tuple[str, Any] | None:
        row = self.db.conn.execute(
            "SELECT status, payload_json, expires_at FROM metadata_cache "
            "WHERE provider=? AND lookup_key=?",
            (self.provider, key),
        ).fetchone()
        if row is None:
            return None
        if not allow_expired and row["expires_at"] < datetime.now(UTC).isoformat():
            return None
        return row["status"], json.loads(row["payload_json"]) if row["payload_json"] else None

    def put(self, key: str, status: str, payload: Any, ttl: timedelta) -> None:
        now = datetime.now(UTC)
        self.db.conn.execute(
            "INSERT OR REPLACE INTO metadata_cache VALUES (?,?,?,?,?,?)",
            (
                self.provider,
                key,
                status,
                json.dumps(payload) if payload is not None else None,
                now.isoformat(timespec="seconds"),
                (now + ttl).isoformat(timespec="seconds"),
            ),
        )


class _HTTPProvider:
    name = ""
    ttl = timedelta(days=90)
    not_found_ttl = timedelta(days=30)

    def __init__(
        self,
        db: Database,
        http: httpx.Client | None,
        *,
        min_interval: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_retries: int = 4,
    ) -> None:
        self.cache = Cache(db, self.name)
        self.http = http
        self.limiter = RateLimiter(min_interval, clock=clock, sleep=sleep)
        self.sleep = sleep
        self.max_retries = max_retries
        self.requests = 0

    def _get(self, url: str, params: dict[str, str]) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            self.requests += 1
            if self.http is None:
                raise ProviderError(f"{self.name}: no network client (cache-only mode)")
            try:
                resp = self.http.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise ProviderError(f"{self.name}: network error: {exc}") from exc
                self.sleep(2**attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 0.0
                # Back off at least 2s, 4s, 8s... even if the server says Retry-After: 0.
                wait = max(wait, 2.0 ** (attempt + 1))
                # Busy servers are routine (MusicBrainz often answers 503); only giving up
                # is worth a warning.
                log.debug("%s returned %s; waiting %.0fs", self.name, resp.status_code, wait)
                self.sleep(min(wait, 60))
                continue
            return resp
        raise ProviderError(f"{self.name}: gave up after {self.max_retries} retries")

    def _ttl_from_headers(self, resp: httpx.Response) -> timedelta:
        m = re.search(r"max-age=(\d+)", resp.headers.get("Cache-Control", ""))
        if m and int(m.group(1)) > 0:
            # Respect the provider's own caching instructions, but keep at least a day so a
            # short max-age doesn't trigger a refetch storm.
            return max(timedelta(seconds=int(m.group(1))), timedelta(days=1))
        return self.ttl


# ---------------------------------------------------------------------------
# MusicBrainz
# ---------------------------------------------------------------------------


class MusicBrainzProvider(_HTTPProvider):
    name = "musicbrainz"

    @staticmethod
    def make_http(contact: str) -> httpx.Client:
        return httpx.Client(
            timeout=30.0,
            headers={
                "User-Agent": f"PlaylistCurator/{__version__} ( {contact} )",
                "Accept": "application/json",
            },
        )

    def __init__(self, db: Database, http: httpx.Client | None, **kw: Any) -> None:
        kw.setdefault("min_interval", 1.1)  # MusicBrainz: ~1 request/second per client
        super().__init__(db, http, **kw)
        self._accepted_inc: dict[str, str] = {}
        self._invalid_streak = 0
        self._logged_rejections: set[str] = set()

    # A run that can't get a single lookup accepted is a request-format problem, not bad data.
    INVALID_STREAK_LIMIT = 15

    def prepare_run(self) -> None:
        """Forget earlier 'invalid' results: they're cheap to retry and were usually caused
        by a request-format problem (fixed since), not by bad ISRCs."""
        self.cache.db.conn.execute(
            "DELETE FROM metadata_cache WHERE provider=? AND status='invalid'", (self.name,)
        )

    def _fetch(self, key: str, path: str, incs: list[str], offline: bool) -> tuple[str, Any] | None:
        """GET a MusicBrainz resource, trying each ``inc`` variant until one is accepted.

        ``inc`` values are space-separated here; httpx encodes spaces as ``+``, which is what
        MusicBrainz expects. (Passing a literal "+" gets encoded as %2B and rejected with 400.)
        """
        cached = self.cache.get(key, allow_expired=offline)
        if cached is not None or offline:
            return cached
        # Start from the variant that worked last time, so a rejected variant costs one
        # extra request per run rather than one per song.
        start = self._accepted_inc.get(incs[0])
        ordered = incs[incs.index(start) :] if start in incs else incs
        resp = None
        for inc in ordered:
            params = {"inc": inc, "fmt": "json"} if inc else {"fmt": "json"}
            resp = self._get(f"{MB_BASE}{path}", params)
            if resp.status_code != 400:
                if resp.status_code in (200, 404):  # the inc value itself was accepted
                    self._accepted_inc[incs[0]] = inc
                break
            if "invalid isrc" in resp.text.lower():
                # The identifier is malformed (Spotify has a few bad ISRCs); other ``inc``
                # variants won't help, and it says nothing about our request format.
                self.cache.put(key, "invalid", None, self.not_found_ttl)
                return "invalid", None
            if inc not in self._logged_rejections:
                self._logged_rejections.add(inc)
                log.warning("MusicBrainz rejected inc=%r: %s", inc, resp.text[:200])
        assert resp is not None
        if resp.status_code == 400:
            self._invalid_streak += 1
            if self._invalid_streak >= self.INVALID_STREAK_LIMIT:
                raise ProviderFatal(
                    f"MusicBrainz rejected the last {self._invalid_streak} lookups as bad "
                    f"requests. Its message: {resp.text[:200]!r}. Please report this."
                )
            # Every variant rejected: the identifier itself is malformed. Skip it for a while.
            self.cache.put(key, "invalid", None, self.not_found_ttl)
            return "invalid", None
        self._invalid_streak = 0
        if resp.status_code == 404:
            self.cache.put(key, "not_found", None, self.not_found_ttl)
            return "not_found", None
        if resp.status_code in (401, 403):
            raise ProviderFatal(
                f"MusicBrainz refused the request (HTTP {resp.status_code}). It may be "
                "blocking this client; wait a while, and check MUSICBRAINZ_CONTACT in .env."
            )
        if resp.status_code != 200:
            raise ProviderError(f"musicbrainz {path}: HTTP {resp.status_code}")
        payload = self._trim(resp.json())
        self.cache.put(key, "ok", payload, self.ttl)
        return "ok", payload

    @staticmethod
    def _trim(body: dict) -> dict:
        def counts(entity: dict) -> dict[str, int]:
            out: dict[str, int] = {}
            for item in (entity.get("tags") or []) + (entity.get("genres") or []):
                name = item.get("name")
                if name:
                    out[name] = max(out.get(name, 0), int(item.get("count") or 0))
            return out

        if "recordings" in body:  # ISRC lookup
            return {
                "recordings": [
                    {
                        "id": r.get("id"),
                        "title": r.get("title"),
                        "first_release_date": r.get("first-release-date"),
                        "tags": counts(r),
                        "artist_ids": [
                            (c.get("artist") or {}).get("id")
                            for c in (r.get("artist-credit") or [])
                            if (c.get("artist") or {}).get("id")
                        ],
                    }
                    for r in body.get("recordings") or []
                ]
            }
        return {"id": body.get("id"), "name": body.get("name"), "tags": counts(body)}

    def lookup(self, q: RecordingQuery, *, offline: bool = False) -> ProviderOutcome | None:
        if not q.isrc:
            return ProviderOutcome(found=False)
        before = self.requests
        res = self._fetch(
            f"isrc:{q.isrc.upper()}",
            f"/isrc/{q.isrc.upper()}",
            # "genres" is left out: it's not accepted on every endpoint, and MusicBrainz
            # returns genres among the tags anyway.
            ["artists tags", "artist-credits tags", "tags", ""],
            offline,
        )
        if res is None:
            return None
        status, payload = res
        if status != "ok" or not payload or not payload["recordings"]:
            return ProviderOutcome(found=False, from_cache=self.requests == before)

        recs = payload["recordings"]
        base = parse_title(q.title).base
        same_title = [r for r in recs if parse_title(r.get("title") or "").base == base]
        chosen = same_title or recs[:1]
        rec_counts: dict[str, int] = {}
        years = []
        for r in chosen:
            for t, c in r["tags"].items():
                rec_counts[t] = max(rec_counts.get(t, 0), c)
            if r.get("first_release_date") and r["first_release_date"][:4].isdigit():
                years.append(int(r["first_release_date"][:4]))

        outcome = ProviderOutcome(found=True)
        outcome.facts["mb_recording_ids"] = [r["id"] for r in chosen if r.get("id")]
        if years:
            outcome.facts["first_release_year"] = min(years)
        if rec_counts:
            outcome.tags["mb_recording"] = weights_from_counts(rec_counts, artist=q.artist)

        artist_ids = chosen[0].get("artist_ids") or []
        if artist_ids:
            ares = self._fetch(
                f"artist:{artist_ids[0]}", f"/artist/{artist_ids[0]}", ["tags", ""], offline
            )
            if ares and ares[0] == "ok" and ares[1] and ares[1]["tags"]:
                outcome.tags["mb_artist"] = weights_from_counts(ares[1]["tags"], artist=q.artist)
        outcome.from_cache = self.requests == before
        return outcome


# ---------------------------------------------------------------------------
# Last.fm
# ---------------------------------------------------------------------------


class LastfmProvider(_HTTPProvider):
    name = "lastfm"
    ttl = timedelta(days=30)

    def __init__(self, db: Database, http: httpx.Client | None, api_key: str, **kw: Any) -> None:
        if http is not None and not api_key:  # cache-only use needs no key
            raise ProviderFatal("LASTFM_API_KEY is not set in .env")
        kw.setdefault("min_interval", 0.25)  # stay well under Last.fm's per-key limits
        super().__init__(db, http, **kw)
        self.api_key = api_key

    @staticmethod
    def make_http() -> httpx.Client:
        return httpx.Client(timeout=30.0, headers={"User-Agent": f"PlaylistCurator/{__version__}"})

    def _call(self, key: str, params: dict[str, str], offline: bool) -> tuple[str, Any] | None:
        cached = self.cache.get(key, allow_expired=offline)
        if cached is not None or offline:
            return cached
        for attempt in range(self.max_retries + 1):
            resp = self._get(LASTFM_BASE, {**params, "api_key": self.api_key, "format": "json"})
            try:
                body = resp.json()
            except ValueError as exc:
                raise ProviderError(f"lastfm: non-JSON response (HTTP {resp.status_code})") from exc
            err = body.get("error") if isinstance(body, dict) else None
            if err is None:
                break
            if err == 6:  # not found
                self.cache.put(key, "not_found", None, self.not_found_ttl)
                return "not_found", None
            if err in (10, 26):
                raise ProviderFatal(f"Last.fm rejected the API key: {body.get('message')}")
            if err in (8, 11, 16, 29) and attempt < self.max_retries:
                self.sleep(min(2 ** (attempt + 1), 60))
                continue
            raise ProviderError(f"Last.fm error {err}: {body.get('message')}")
        tags = (body.get("toptags") or {}).get("tag") or []
        if isinstance(tags, dict):  # Last.fm returns an object when there is one tag
            tags = [tags]
        payload = {
            "tags": {t["name"]: int(t.get("count") or 0) for t in tags[:40] if t.get("name")}
        }
        self.cache.put(key, "ok", payload, self._ttl_from_headers(resp))
        return "ok", payload

    def lookup(self, q: RecordingQuery, *, offline: bool = False) -> ProviderOutcome | None:
        if not q.artist or not q.title:
            return ProviderOutcome(found=False)
        before = self.requests
        artist_key = normalize_artist(q.artist)
        track_key = f"track:{artist_key}|{parse_title(q.title).base}"
        res = self._call(
            track_key,
            {
                "method": "track.gettoptags",
                "artist": q.artist,
                "track": q.title,
                "autocorrect": "1",
            },
            offline,
        )
        ares = self._call(
            f"artist:{artist_key}",
            {"method": "artist.gettoptags", "artist": q.artist, "autocorrect": "1"},
            offline,
        )
        if res is None and ares is None:
            return None
        outcome = ProviderOutcome(found=False)
        if res and res[0] == "ok" and res[1] and res[1]["tags"]:
            # the song's own tags: keep up to 40 and weaker ones too, for a fuller picture
            outcome.tags["lastfm_track"] = weights_from_counts(
                res[1]["tags"], artist=q.artist, top=40, min_weight=0.05
            )
        if ares and ares[0] == "ok" and ares[1] and ares[1]["tags"]:
            outcome.tags["lastfm_artist"] = weights_from_counts(ares[1]["tags"], artist=q.artist)
        outcome.tags = {k: v for k, v in outcome.tags.items() if v}
        outcome.found = bool(outcome.tags)
        outcome.from_cache = self.requests == before
        return outcome


# ---------------------------------------------------------------------------
# AcousticBrainz: audio-derived measures of the song itself
# ---------------------------------------------------------------------------

# classifier -> (class whose probability we read, vibe word it becomes)
AB_CLASSIFIERS: dict[str, tuple[str, str]] = {
    "danceability": ("danceable", "danceable"),
    "mood_relaxed": ("relaxed", "relaxed"),
    "mood_happy": ("happy", "happy"),
    "mood_sad": ("sad", "sad"),
    "mood_aggressive": ("aggressive", "aggressive"),
    "mood_party": ("party", "party"),
    "mood_acoustic": ("acoustic", "acoustic"),
    "mood_electronic": ("electronic", "synth"),
    "voice_instrumental": ("instrumental", "instrumental"),
}
# multi-class classifiers: class -> vibe word (read when that class is the confident winner)
AB_MULTI: dict[str, dict[str, str]] = {
    "timbre": {"bright": "bright", "dark": "dark"},
    # only meaningful when the song has vocals (checked against voice_instrumental)
    "gender": {"female": "female vocals", "male": "male vocals"},
    # MIREX mood clusters (passionate/rousing, cheerful/fun, poignant/bittersweet,
    # humorous/whimsical, aggressive/fiery)
    "moods_mirex": {
        "Cluster1": "passionate",
        "Cluster2": "cheerful",
        "Cluster3": "bittersweet",
        "Cluster4": "playful",
        "Cluster5": "intense",
    },
}
AB_MULTI_MIN = {"timbre": 0.7, "gender": 0.75, "moods_mirex": 0.45}
# low-level measurements (not classifiers): tempo, key, how busy and how dynamic
AB_LOWLEVEL = (
    "rhythm.bpm",
    "tonal.key_scale",
    "tonal.key_strength",
    "rhythm.onset_rate",
    "lowlevel.dynamic_complexity",
)
AB_MEASURE_WEIGHT = 0.5  # measurements are reliable-ish but say less about the feel
AB_WEIGHT = 0.7  # audio classifiers are useful but noisy: they count less than song tags
AB_MIN_PROBABILITY = 0.6  # below this the classifier isn't saying much either way
AB_BULK = 25  # documented maximum recording ids per request


def ab_vibes(features: dict[str, Any]) -> dict[str, float]:
    """AcousticBrainz readings -> vibe weights. Only confident readings count; every rule is a
    fixed threshold on a published measure, nothing is learned."""
    out: dict[str, float] = {}

    def put(word: str, w: float) -> None:
        if w > 0:
            out[word] = max(out.get(word, 0.0), round(w, 4))

    for _clf, (cls, word) in AB_CLASSIFIERS.items():
        p = features.get(cls)
        if isinstance(p, int | float) and p >= AB_MIN_PROBABILITY:
            put(word, AB_WEIGHT * (p - 0.5) / 0.5)
    for clf, words in AB_MULTI.items():
        probs = features.get(clf)
        if not isinstance(probs, dict) or not probs:
            continue
        if clf == "gender" and (features.get("instrumental") or 0) > 0.5:
            continue  # no vocals to speak of
        cls, p = max(probs.items(), key=lambda kv: kv[1])
        if cls in words and p >= AB_MULTI_MIN[clf]:
            put(words[cls], AB_WEIGHT * p)
    bpm = features.get("bpm")
    if isinstance(bpm, int | float) and bpm > 0:
        # detected tempo; half/double-time mistakes happen, hence the low weight
        if bpm < 85:
            put("slow", AB_MEASURE_WEIGHT)
        elif bpm <= 115:
            put("mid-tempo", AB_MEASURE_WEIGHT)
        elif bpm >= 135:
            put("fast", AB_MEASURE_WEIGHT)
        else:
            put("upbeat", AB_MEASURE_WEIGHT * 0.6)
    scale, strength = features.get("key_scale"), features.get("key_strength") or 0
    if scale in ("major", "minor") and strength >= 0.6:
        put(f"{scale} key", AB_MEASURE_WEIGHT)
    onset = features.get("onset_rate")
    if isinstance(onset, int | float):
        if onset >= 5.5:
            put("busy", AB_MEASURE_WEIGHT * 0.8)
        elif onset <= 2.0:
            put("minimal", AB_MEASURE_WEIGHT * 0.8)
    dyn = features.get("dynamic_complexity")
    if isinstance(dyn, int | float) and dyn >= 6:
        put("dynamic", AB_MEASURE_WEIGHT * 0.8)
    return out


class AcousticBrainzProvider(_HTTPProvider):
    """High-level audio descriptors from AcousticBrainz, looked up by MusicBrainz recording id.

    AcousticBrainz analysed audio files submitted by its community and stopped collecting in
    2022; its data (CC0) and API remain available. Songs need a MusicBrainz match first (run
    MusicBrainz before this). Nothing from Spotify is sent: only MusicBrainz ids.
    """

    name = "acousticbrainz"
    ttl = timedelta(days=365)  # the dataset no longer changes
    not_found_ttl = timedelta(days=180)

    def __init__(self, db: Database, http: httpx.Client | None, **kw: Any) -> None:
        kw.setdefault("min_interval", 1.0)  # documented typical limit: 10 requests / 10 s
        super().__init__(db, http, **kw)
        self._mb = MusicBrainzProvider(db, None, min_interval=0)

    @staticmethod
    def make_http(contact: str) -> httpx.Client:
        return httpx.Client(
            timeout=30.0,
            headers={"User-Agent": f"PlaylistCurator/{__version__} ( {contact} )"},
        )

    def _mbids(self, q: RecordingQuery) -> list[str]:
        out = self._mb.lookup(q, offline=True)
        if out is None or not out.found:
            return []
        return [m.lower() for m in out.facts.get("mb_recording_ids", [])][:3]

    def _fetch_bulk(self, mbids: list[str]) -> None:
        resp = self._get(f"{AB_BASE}/high-level", {"recording_ids": ";".join(mbids)})
        if resp.status_code != 200:
            raise ProviderError(f"acousticbrainz: HTTP {resp.status_code}")
        body = resp.json()
        for mbid in mbids:
            docs = body.get(mbid) or {}
            doc = docs.get("0") or (next(iter(docs.values())) if docs else None)
            hl = (doc or {}).get("highlevel") or {}
            feats: dict[str, Any] = {}
            for clf, (cls, _word) in AB_CLASSIFIERS.items():
                p = ((hl.get(clf) or {}).get("all") or {}).get(cls)
                if isinstance(p, int | float):
                    feats[cls] = round(float(p), 4)
            for clf in AB_MULTI:
                probs = (hl.get(clf) or {}).get("all") or {}
                probs = {
                    k: round(float(v), 4) for k, v in probs.items() if isinstance(v, int | float)
                }
                if probs:
                    feats[clf] = probs
            if feats:
                self.cache.put(f"mbid:{mbid}", "ok", {"features": feats}, self.ttl)
            else:
                self.cache.put(f"mbid:{mbid}", "not_found", None, self.not_found_ttl)
        self._respect_rate_headers(resp)

    def _fetch_lowlevel(self, mbids: list[str]) -> None:
        resp = self._get(
            f"{AB_BASE}/low-level",
            {"recording_ids": ";".join(mbids), "features": ";".join(AB_LOWLEVEL)},
        )
        if resp.status_code != 200:
            raise ProviderError(f"acousticbrainz low-level: HTTP {resp.status_code}")
        body = resp.json()
        for mbid in mbids:
            docs = body.get(mbid) or {}
            doc = docs.get("0") or (next(iter(docs.values())) if docs else None) or {}
            rhythm, tonal, low = (doc.get(k) or {} for k in ("rhythm", "tonal", "lowlevel"))
            meas = {
                "bpm": rhythm.get("bpm"),
                "onset_rate": rhythm.get("onset_rate"),
                "key_scale": tonal.get("key_scale"),
                "key_key": tonal.get("key_key"),
                "key_strength": tonal.get("key_strength"),
                "dynamic_complexity": low.get("dynamic_complexity"),
            }
            meas = {k: v for k, v in meas.items() if v is not None}
            if meas:
                self.cache.put(f"ll:{mbid}", "ok", meas, self.ttl)
            else:
                self.cache.put(f"ll:{mbid}", "not_found", None, self.not_found_ttl)
        self._respect_rate_headers(resp)

    def _respect_rate_headers(self, resp: httpx.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset-In")
        if remaining == "0" and reset and reset.isdigit():
            self.sleep(min(float(reset) + 0.5, 60))

    def prefetch(self, queries: list[RecordingQuery], progress: Callable[[str], None]) -> None:
        """Fetch everything not cached yet, 25 recordings per request."""
        hl_todo, ll_todo = [], []
        for q in queries:
            for m in self._mbids(q):
                if self.cache.get(f"mbid:{m}") is None:
                    hl_todo.append(m)
                if self.cache.get(f"ll:{m}") is None:
                    ll_todo.append(m)
        hl_todo, ll_todo = list(dict.fromkeys(hl_todo)), list(dict.fromkeys(ll_todo))
        if hl_todo or ll_todo:
            progress(f"acousticbrainz: {max(len(hl_todo), len(ll_todo))} recordings to fetch")
        for i in range(0, len(hl_todo), AB_BULK):
            self._fetch_bulk(hl_todo[i : i + AB_BULK])
        for i in range(0, len(ll_todo), AB_BULK):
            self._fetch_lowlevel(ll_todo[i : i + AB_BULK])

    def lookup(self, q: RecordingQuery, *, offline: bool = False) -> ProviderOutcome | None:
        mbids = self._mbids(q)
        if not mbids:
            return ProviderOutcome(found=False)
        before = self.requests
        found: list[dict[str, Any]] = []
        for m in mbids:
            feats: dict[str, Any] = {}
            for prefix, fetch in (("mbid", self._fetch_bulk), ("ll", self._fetch_lowlevel)):
                cached = self.cache.get(f"{prefix}:{m}", allow_expired=offline)
                if cached is None and not offline:
                    fetch([m])
                    cached = self.cache.get(f"{prefix}:{m}")
                if cached and cached[0] == "ok" and cached[1]:
                    feats.update(cached[1]["features"] if prefix == "mbid" else cached[1])
            if feats:
                found.append(feats)
        if not found:
            return ProviderOutcome(found=False, from_cache=self.requests == before)
        # several MusicBrainz recordings of the same song: use the first one AcousticBrainz has
        # (averaging tempos or keys across different recordings would invent values)
        feats = found[0]
        outcome = ProviderOutcome(found=True, from_cache=self.requests == before)
        tags = ab_vibes(feats)
        if tags:
            outcome.tags["acousticbrainz"] = tags
        outcome.facts["ab_features"] = {
            k: (round(v, 3) if isinstance(v, float) else v)
            for k, v in feats.items()
            if not isinstance(v, dict)
        }
        return outcome
