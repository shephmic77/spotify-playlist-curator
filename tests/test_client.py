import httpx
import pytest

from playlist_curator.spotify.client import RateLimitedError, SpotifyAPIError, SpotifyClient


def test_pagination_follows_next_links(fake, client, small_pages):
    items = list(client.iter_saved_tracks())
    assert len(items) == len(fake.lib["liked"])
    assert fake.count("/me/tracks") == 2  # 4 items, page size 2


def test_pagination_preserves_market_param(fake, client, small_pages):
    urls: list[str] = []
    fake.on_request = lambda r: urls.append(str(r.url))
    list(client.iter_playlist_items("pl_80s"))
    assert len(urls) == 2
    assert all("market=from_token" in u for u in urls)
    assert all("additional_types=track%2Cepisode" in u for u in urls)


def test_401_triggers_single_refresh_then_retry(fake, client, tokens):
    fake.valid_tokens = {"access-2"}  # current token access-1 is "expired"
    assert client.me()["id"] == "test_user"
    assert tokens.refreshes == 1


def test_persistent_401_raises_after_one_refresh(fake, client, tokens):
    fake.valid_tokens = set()
    with pytest.raises(SpotifyAPIError) as exc:
        client.me()
    assert exc.value.status == 401
    assert tokens.refreshes == 1


def test_429_honours_retry_after(fake, client, sleeps):
    fake.inject("/me", 429, {"Retry-After": "3"})
    assert client.me()["id"] == "test_user"
    assert sleeps == [3.0]


def test_long_retry_after_raises_instead_of_hanging(fake, client, sleeps):
    fake.inject("/me", 429, {"Retry-After": "86400"})
    with pytest.raises(RateLimitedError) as exc:
        client.me()
    assert exc.value.retry_after == 86400
    assert sleeps == []


def test_5xx_is_retried_with_backoff(fake, client, sleeps):
    fake.inject("/me", 503, times=2)
    assert client.me()["id"] == "test_user"
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_5xx_gives_up_after_max_retries(fake, tokens, sleeps):
    c = SpotifyClient(tokens, fake.http(), max_retries=2, sleep=sleeps.append, backoff_base=0)
    fake.inject("/me", 500, times=5)
    with pytest.raises(SpotifyAPIError) as exc:
        c.me()
    assert exc.value.status == 500
    assert len(sleeps) == 2


def test_network_errors_are_retried(tokens, sleeps):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"id": "u"})

    c = SpotifyClient(
        tokens,
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        backoff_base=0,
    )
    assert c.me() == {"id": "u"}
    assert attempts["n"] == 2


def test_4xx_error_message_is_surfaced(fake, client):
    with pytest.raises(SpotifyAPIError) as exc:
        list(client.iter_playlist_items("pl_followed"))
    assert exc.value.status == 403
    assert "Forbidden" in str(exc.value)
    assert "/playlists/pl_followed/items" in str(exc.value)
    assert "Bearer" not in str(exc.value)


def test_add_items_chunks_at_100(fake, client):
    uris = [f"spotify:track:t{i}" for i in range(250)]
    client.add_items("pl_80s", uris)
    bodies = [b for m, p, b in fake.write_bodies if m == "POST"]
    assert [len(b["uris"]) for b in bodies] == [100, 100, 50]
    assert [u for b in bodies for u in b["uris"]] == uris


def test_remove_items_uses_items_key_and_snapshot(fake, client):
    client.remove_items("pl_80s", ["spotify:track:a", "spotify:track:b"], snapshot_id="s1")
    method, path, body = fake.write_bodies[-1]
    assert (method, path) == ("DELETE", "/playlists/pl_80s/items")
    assert body == {
        "items": [{"uri": "spotify:track:a"}, {"uri": "spotify:track:b"}],
        "snapshot_id": "s1",
    }


def test_replace_items_puts_first_chunk_then_appends(fake, client):
    uris = [f"spotify:track:t{i}" for i in range(150)]
    client.replace_items("pl_80s", uris)
    assert [(m, len(b["uris"])) for m, _, b in fake.write_bodies] == [("PUT", 100), ("POST", 50)]


def test_create_playlist_defaults_to_private(fake, client):
    client.create_playlist("New", "desc")
    _, path, body = fake.write_bodies[-1]
    assert path == "/me/playlists"
    assert body == {"name": "New", "public": False, "description": "desc"}


def test_writes_refuse_local_files(client):
    with pytest.raises(ValueError, match="local-file"):
        client.add_items("pl_80s", ["spotify:local:a:b:c:100"])
    with pytest.raises(ValueError):
        client.add_items("pl_80s", ["not-a-uri"])
