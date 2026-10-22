from __future__ import annotations

import pytest

from fake_spotify import FakeSpotify, StaticTokens
from playlist_curator.spotify import client as client_module
from playlist_curator.spotify.client import SpotifyClient
from playlist_curator.storage.database import Database


@pytest.fixture
def fake() -> FakeSpotify:
    return FakeSpotify()


@pytest.fixture
def small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force multi-page responses from the small fixture library."""
    monkeypatch.setattr(client_module, "PAGE_LIMIT", 2)


@pytest.fixture
def tokens() -> StaticTokens:
    return StaticTokens()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(fake: FakeSpotify, tokens: StaticTokens, sleeps: list[float]) -> SpotifyClient:
    fake.valid_tokens.add("access-2")
    return SpotifyClient(tokens, fake.http(), sleep=sleeps.append, backoff_base=0.01)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never talk to real services: any request not served by a fake fails at once."""
    import httpx

    real = httpx.HTTPTransport.handle_request

    def refuse(self, request):
        if request.url.host in ("127.0.0.1", "localhost", "::1"):  # the local login callback
            return real(self, request)
        raise RuntimeError(f"test tried to reach the real network: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
