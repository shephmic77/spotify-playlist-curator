"""Thin, resilient Spotify Web API client.

Responsibilities: auth headers, token refresh on 401, 429 handling with ``Retry-After``,
exponential backoff for transient failures, pagination via ``next`` links, and clear errors.
It returns raw JSON; normalization lives in ``playlist_curator.models``.

Only endpoints verified against the post-February-2026 documentation are used:
``GET /me``, ``GET /me/tracks``, ``GET /me/playlists``, ``GET /playlists/{id}``,
``GET /playlists/{id}/items``, ``POST /me/playlists``, ``PUT /playlists/{id}``,
``POST|PUT|DELETE /playlists/{id}/items``. See docs/spotify_api.md.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.spotify.com/v1"
PAGE_LIMIT = 50  # documented maximum for /me/tracks, /me/playlists, /playlists/{id}/items
WRITE_CHUNK = 100  # documented maximum URIs per playlist add/remove/replace request
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


class TokenProvider(Protocol):
    def access_token(self) -> str: ...
    def force_refresh(self) -> str: ...


class SpotifyAPIError(RuntimeError):
    def __init__(
        self,
        status: int,
        method: str,
        url: str,
        message: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        self.status = status
        self.method = method
        self.url = url
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"Spotify API {method} {url} failed ({status}): {message}")


class RateLimitedError(SpotifyAPIError):
    """429 with a Retry-After longer than we are willing to sleep for."""


def _redact(url: str) -> str:
    return url.replace(API_BASE, "")


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300] or resp.reason_phrase
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)
    if isinstance(err, str):
        return f"{err}: {body.get('error_description', '')}".strip(": ")
    return str(body)[:300]


class SpotifyClient:
    def __init__(
        self,
        tokens: TokenProvider,
        http: httpx.Client | None = None,
        *,
        max_retries: int = 5,
        max_retry_after: float = 120.0,
        backoff_base: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tokens = tokens
        self.http = http or httpx.Client(base_url=API_BASE, timeout=30.0)
        self.max_retries = max_retries
        self.max_retry_after = max_retry_after
        self.backoff_base = backoff_base
        self.sleep = sleep
        self.request_count = 0

    def close(self) -> None:
        self.http.close()

    # -- core --------------------------------------------------------------

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            return path_or_url
        return f"{API_BASE}{path_or_url}"

    def _backoff(self, attempt: int) -> float:
        return self.backoff_base * (2**attempt) + random.uniform(0, self.backoff_base)

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        url = self._url(path_or_url)
        refreshed = False
        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {self.tokens.access_token()}"}
            try:
                self.request_count += 1
                resp = self.http.request(method, url, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise SpotifyAPIError(0, method, _redact(url), f"network error: {exc}") from exc
                wait = self._backoff(attempt)
                log.warning(
                    "Network error on %s %s (%s); retrying in %.1fs",
                    method,
                    _redact(url),
                    exc,
                    wait,
                )
                self.sleep(wait)
                attempt += 1
                continue

            status = resp.status_code
            if status == 401 and not refreshed:
                log.debug("401 from Spotify; refreshing token and retrying once")
                self.tokens.force_refresh()
                refreshed = True
                continue

            if status == 429:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                wait = retry_after if retry_after is not None else self._backoff(attempt)
                if wait > self.max_retry_after or attempt >= self.max_retries:
                    raise RateLimitedError(
                        429,
                        method,
                        _redact(url),
                        f"rate limited; Spotify asked to wait {wait:.0f}s. "
                        "Development Mode apps have low limits; try again later.",
                        retry_after=wait,
                    )
                log.warning("Rate limited on %s; waiting %.1fs", _redact(url), wait)
                self.sleep(wait)
                attempt += 1
                continue

            if status in RETRYABLE_STATUS and attempt < self.max_retries:
                wait = self._backoff(attempt)
                log.warning("Spotify %s on %s; retrying in %.1fs", status, _redact(url), wait)
                self.sleep(wait)
                attempt += 1
                continue

            if status >= 400:
                raise SpotifyAPIError(status, method, _redact(url), _error_message(resp))

            if status == 204 or not resp.content:
                return {}
            return resp.json()

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Yield every element of ``items`` across pages, following ``next`` links."""
        page = self.request("GET", path, params={"limit": PAGE_LIMIT, **(params or {})})
        seen_urls: set[str] = set()
        while True:
            yield from page.get("items") or []
            next_url = page.get("next")
            if not next_url or next_url in seen_urls:
                return
            seen_urls.add(next_url)
            # ``next`` already carries limit/offset/market; don't resend params.
            page = self.request("GET", next_url)

    # -- read endpoints ----------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self.request("GET", "/me")

    def iter_saved_tracks(self) -> Iterator[dict]:
        # market=from_token: since Feb 2026 /me no longer returns country, so we let Spotify
        # use the token's market for playability/relinking instead of passing one.
        return self.paginate("/me/tracks", {"market": "from_token"})

    def iter_my_playlists(self) -> Iterator[dict]:
        return self.paginate("/me/playlists")

    def get_playlist(self, playlist_id: str, fields: str | None = None) -> dict[str, Any]:
        params = {"fields": fields} if fields else None
        return self.request("GET", f"/playlists/{playlist_id}", params=params)

    def iter_playlist_items(self, playlist_id: str) -> Iterator[dict]:
        # Ask for episodes too so they come back typed and can be skipped explicitly.
        return self.paginate(
            f"/playlists/{playlist_id}/items",
            {"market": "from_token", "additional_types": "track,episode"},
        )

    # -- write endpoints (used from Phase 6; never called by the Phase 1 importer) --------

    def create_playlist(
        self, name: str, description: str | None = None, *, public: bool = False
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "public": public}
        if description:
            body["description"] = description
        return self.request("POST", "/me/playlists", json=body)

    def update_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        public: bool | None = None,
    ) -> None:
        body = {
            k: v
            for k, v in {"name": name, "description": description, "public": public}.items()
            if v is not None
        }
        if body:
            self.request("PUT", f"/playlists/{playlist_id}", json=body)

    def add_items(
        self, playlist_id: str, uris: Iterable[str], position: int | None = None
    ) -> str | None:
        """Append (or insert at ``position``) catalog URIs. Returns the last snapshot_id."""
        uri_list = _catalog_uris(uris)
        snapshot = None
        for i, chunk in enumerate(_chunks(uri_list, WRITE_CHUNK)):
            body: dict[str, Any] = {"uris": chunk}
            if position is not None:
                body["position"] = position + i * WRITE_CHUNK
            snapshot = self.request("POST", f"/playlists/{playlist_id}/items", json=body).get(
                "snapshot_id"
            )
        return snapshot

    def replace_items(self, playlist_id: str, uris: Iterable[str]) -> str | None:
        """Replace all items: PUT the first chunk, then append the rest."""
        uri_list = _catalog_uris(uris)
        first, rest = uri_list[:WRITE_CHUNK], uri_list[WRITE_CHUNK:]
        snapshot = self.request("PUT", f"/playlists/{playlist_id}/items", json={"uris": first}).get(
            "snapshot_id"
        )
        if rest:
            snapshot = self.add_items(playlist_id, rest)
        return snapshot

    def remove_playlists_from_library(self, playlist_ids: Iterable[str]) -> None:
        """Remove playlists from your library (Spotify's way of deleting your own playlist).

        ``DELETE /me/library`` also removes saved *songs* when given track URIs, so this method
        only ever builds ``spotify:playlist:`` URIs from bare playlist ids and refuses anything
        that isn't one. It can't touch Liked Songs."""
        ids = list(playlist_ids)
        bad = [p for p in ids if not _PLAYLIST_ID.match(p)]
        if bad:
            raise ValueError(f"Not playlist ids: {bad[:3]}")
        for chunk in _chunks(ids, LIBRARY_REMOVE_CHUNK):
            self.request(
                "DELETE",
                "/me/library",
                params={"uris": ",".join(f"spotify:playlist:{p}" for p in chunk)},
            )

    def remove_items(
        self, playlist_id: str, uris: Iterable[str], snapshot_id: str | None = None
    ) -> str | None:
        """Remove every occurrence of each URI. Returns the last snapshot_id."""
        uri_list = _catalog_uris(uris)
        snapshot = snapshot_id
        for chunk in _chunks(uri_list, WRITE_CHUNK):
            body: dict[str, Any] = {"items": [{"uri": u} for u in chunk]}
            if snapshot:
                body["snapshot_id"] = snapshot
            snapshot = self.request("DELETE", f"/playlists/{playlist_id}/items", json=body).get(
                "snapshot_id"
            )
        return snapshot


LIBRARY_REMOVE_CHUNK = 40  # documented maximum URIs per DELETE /me/library
_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_]+$")


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _catalog_uris(uris: Iterable[str]) -> list[str]:
    """Writes accept catalog URIs only. Local files cannot be added via the Web API."""
    out = list(uris)
    local = [u for u in out if u.startswith("spotify:local:")]
    if local:
        raise ValueError(
            f"{len(local)} local-file URI(s) cannot be written via the Web API; "
            "they must be added manually in the Spotify desktop app."
        )
    bad = [u for u in out if not u.startswith(("spotify:track:", "spotify:episode:"))]
    if bad:
        raise ValueError(f"Not Spotify track URIs: {bad[:3]}")
    return out
