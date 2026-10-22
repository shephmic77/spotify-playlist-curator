"""Fakes of the MusicBrainz, Last.fm and AcousticBrainz endpoints used for descriptors."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

# ISRC -> MusicBrainz recordings (subset of real response shape)
MB_ISRC: dict[str, list[dict[str, Any]]] = {
    "FRZID1100001": [
        {
            "id": "mb-rec-midnight",
            "title": "Midnight City",
            "first-release-date": "2011-08-16",
            "tags": [{"name": "synthpop", "count": 4}, {"name": "seen live", "count": 9}],
            "genres": [{"name": "electronic", "count": 3}],
            "artist-credit": [{"artist": {"id": "mb-art-m83", "name": "M83"}}],
        }
    ],
    "GBAYE8500001": [
        {
            "id": "mb-rec-tom",
            "title": "Take On Me",
            "first-release-date": "1984-10-19",
            "tags": [{"name": "synth-pop", "count": 5}, {"name": "new wave", "count": 3}],
            "genres": [],
            "artist-credit": [{"artist": {"id": "mb-art-aha", "name": "a-ha"}}],
        },
        {  # an unrelated recording sharing the ISRC (happens in real data)
            "id": "mb-rec-other",
            "title": "Something Else",
            "tags": [{"name": "polka", "count": 9}],
            "artist-credit": [],
        },
    ],
    "USSM19801234": [
        {
            "id": "mb-rec-africa",
            "title": "Africa",
            "first-release-date": "1982-04-08",
            "tags": [{"name": "soft rock", "count": 2}],
            "artist-credit": [{"artist": {"id": "mb-art-toto", "name": "TOTO"}}],
        }
    ],
}
MB_ARTIST: dict[str, list[dict[str, Any]]] = {
    "mb-art-m83": [{"name": "electronic", "count": 5}, {"name": "dream pop", "count": 3}],
    "mb-art-aha": [{"name": "new wave", "count": 4}, {"name": "80s", "count": 2}],
    "mb-art-toto": [{"name": "rock", "count": 3}],
}

# (artist lower, track lower) -> [(tag, count)]
LASTFM_TRACK: dict[tuple[str, str], list[tuple[str, int]]] = {
    ("m83", "midnight city"): [
        ("synthwave", 100),
        ("80s", 60),
        ("electronic", 55),
        ("favorites", 40),
    ],
    ("a-ha", "take on me"): [("80s", 100), ("synthpop", 90), ("new wave", 70)],
    ("toto", "africa"): [("80s", 100), ("soft rock", 85), ("yacht rock", 20)],
    ("kavinsky", "nightcall"): [("synthwave", 100), ("80s", 70), ("Drive", 50)],
    ("claude debussy", "clair de lune"): [("classical", 100), ("piano", 90)],
}
LASTFM_ARTIST: dict[str, list[tuple[str, int]]] = {
    "m83": [("electronic", 100), ("shoegaze", 40)],
    "kavinsky": [("synthwave", 100)],
    "claude debussy": [("classical", 100), ("impressionism", 50)],
}


# MusicBrainz recording id -> AcousticBrainz high-level document (real shape, trimmed)
def _hl(**probs: float) -> dict[str, Any]:
    pos = {
        "danceability": "danceable",
        "mood_relaxed": "relaxed",
        "mood_happy": "happy",
        "mood_sad": "sad",
        "mood_aggressive": "aggressive",
        "mood_party": "party",
        "mood_acoustic": "acoustic",
        "mood_electronic": "electronic",
        "voice_instrumental": "instrumental",
    }
    hl = {}
    for clf, cls in pos.items():
        p = probs.get(cls, 0.3)
        neg = "voice" if cls == "instrumental" else f"not_{cls}"
        hl[clf] = {
            "all": {cls: p, neg: round(1 - p, 3)},
            "probability": max(p, 1 - p),
            "value": cls if p >= 0.5 else neg,
        }
    return {"highlevel": hl, "metadata": {"version": {"essentia": "2.1-beta2"}}}


AB_HIGHLEVEL: dict[str, dict[str, Any]] = {
    "mb-rec-midnight": _hl(danceable=0.9, electronic=0.95, party=0.7),
    "mb-rec-tom": _hl(danceable=0.8, happy=0.85),
    # mb-rec-africa: not in AcousticBrainz
}
AB_HIGHLEVEL["mb-rec-midnight"]["highlevel"]["moods_mirex"] = {
    "all": {"Cluster1": 0.6, "Cluster2": 0.1, "Cluster3": 0.1, "Cluster4": 0.1, "Cluster5": 0.1}
}
AB_HIGHLEVEL["mb-rec-midnight"]["highlevel"]["gender"] = {"all": {"female": 0.1, "male": 0.9}}
AB_HIGHLEVEL["mb-rec-midnight"]["highlevel"]["timbre"] = {"all": {"bright": 0.55, "dark": 0.45}}
AB_LOWLEVEL: dict[str, dict[str, Any]] = {
    "mb-rec-midnight": {
        "rhythm": {"bpm": 105.2, "onset_rate": 4.1},
        "tonal": {"key_key": "E", "key_scale": "major", "key_strength": 0.71},
        "lowlevel": {"dynamic_complexity": 3.2},
    },
    "mb-rec-tom": {"rhythm": {"bpm": 168.9}, "tonal": {"key_scale": "minor", "key_strength": 0.4}},
}


class FakeMetadataAPIs:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.user_agents: list[str] = []
        self.injected: list[httpx.Response] = []
        self.lastfm_error: int | None = None
        self.cache_control: str | None = None
        self.raw_queries: list[str] = []
        # Modelled on the live behaviour seen 2026-10-18: "genres" is rejected on ISRC lookups.
        self.isrc_inc = {"artists", "tags"}
        self.malformed_isrcs: set[str] = set()
        self.forbidden: set[str] = set()

    def http(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append(f"{url.netloc}{url.path}?{q.get('method', q.get('inc', ''))}")
        self.user_agents.append(request.headers.get("User-Agent", ""))
        if self.injected:
            return self.injected.pop(0)
        if url.netloc == "musicbrainz.org":
            assert q.get("fmt") == "json"
            self.raw_queries.append(url.query)
            parts = url.path.split("/")
            kind, ident = parts[-2], parts[-1]
            # Like the real API: inc values are space-separated ("+" in the raw query). A
            # literal "+" (sent as %2B) or an unknown value is a 400 Bad Request.
            allowed = self.isrc_inc if kind == "isrc" else {"tags"}
            if any(v not in allowed for v in q.get("inc", "").split(" ") if v):
                return httpx.Response(400, json={"error": "Invalid inc"})
            if ident in self.malformed_isrcs:
                return httpx.Response(400, json={"error": "Invalid isrc"})
            if ident in self.forbidden:
                return httpx.Response(403, json={"error": "blocked"})
            if kind == "isrc" and ident in MB_ISRC:
                return httpx.Response(200, json={"isrc": ident, "recordings": MB_ISRC[ident]})
            if kind == "artist" and ident in MB_ARTIST:
                return httpx.Response(
                    200, json={"id": ident, "name": "x", "tags": MB_ARTIST[ident], "genres": []}
                )
            return httpx.Response(404, json={"error": "Not Found"})
        if url.netloc == "acousticbrainz.org":
            ids = q.get("recording_ids", "").split(";")
            assert 0 < len(ids) <= 25
            if url.path == "/api/v1/low-level":
                self.raw_queries.append(url.query)
                body = {m: {"0": AB_LOWLEVEL[m]} for m in ids if m in AB_LOWLEVEL}
            else:
                assert url.path == "/api/v1/high-level"
                body = {m: {"0": AB_HIGHLEVEL[m]} for m in ids if m in AB_HIGHLEVEL}
            return httpx.Response(
                200,
                json=body,
                headers={"X-RateLimit-Remaining": "9", "X-RateLimit-Reset-In": "10"},
            )
        if url.netloc == "ws.audioscrobbler.com":
            assert q.get("api_key") and q.get("format") == "json"
            if self.lastfm_error is not None:
                code, self.lastfm_error = self.lastfm_error, None
                return httpx.Response(200, json={"error": code, "message": f"error {code}"})
            headers = {"Cache-Control": self.cache_control} if self.cache_control else {}
            artist = q.get("artist", "").lower()
            if q["method"] == "track.gettoptags":
                tags = LASTFM_TRACK.get((artist, q.get("track", "").lower()))
            else:
                tags = LASTFM_ARTIST.get(artist)
            if tags is None:
                return httpx.Response(200, json={"error": 6, "message": "not found"})
            items = [{"name": n, "count": c, "url": ""} for n, c in tags]
            body = {
                "toptags": {
                    "tag": items[0] if len(items) == 1 else items,
                    "@attr": {"artist": artist},
                }
            }
            return httpx.Response(200, json=body, headers=headers)
        return httpx.Response(404)
