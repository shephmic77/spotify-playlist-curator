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
                log.warning("%s returned %s; waiting %.0fs", self.name, resp.status_code, wait)
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
            outcome.tags["lastfm_track"] = weights_from_counts(res[1]["tags"], artist=q.artist)
        if ares and ares[0] == "ok" and ares[1] and ares[1]["tags"]:
            outcome.tags["lastfm_artist"] = weights_from_counts(ares[1]["tags"], artist=q.artist)
        outcome.tags = {k: v for k, v in outcome.tags.items() if v}
        outcome.found = bool(outcome.tags)
        outcome.from_cache = self.requests == before
        return outcome
