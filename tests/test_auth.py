import base64
import hashlib
import json
import os
import stat
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from playlist_curator.config import ConfigError, Settings
from playlist_curator.spotify import auth
from playlist_curator.spotify.auth import (
    AuthError,
    NotLoggedInError,
    TokenManager,
    TokenSet,
    TokenStore,
    build_authorize_url,
    code_challenge_s256,
    generate_code_verifier,
    login,
    parse_callback_url,
)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(client_id="client-123", data_dir=tmp_path)


def token_endpoint(responses: list[dict], seen: list[dict]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == auth.TOKEN_URL
        seen.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_verifier_and_s256_challenge():
    v = generate_code_verifier()
    assert 43 <= len(v) <= 128
    assert set(v) <= set(auth._VERIFIER_ALPHABET)
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert code_challenge_s256(v) == expected
    assert "=" not in code_challenge_s256(v) and "+" not in code_challenge_s256(v)
    with pytest.raises(ValueError):
        generate_code_verifier(20)


def test_authorize_url_has_required_params(settings):
    url = build_authorize_url(settings, "chal", "st")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["response_type"] == "code"
    assert q["client_id"] == "client-123"
    assert q["redirect_uri"] == "http://127.0.0.1:8888/callback"
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == "chal"
    assert q["state"] == "st"
    assert "playlist-read-private" in q["scope"].split()
    scopes = set(q["scope"].split())
    # Phase 6: may create/update its own playlists; never anything that changes Liked Songs
    assert {"playlist-modify-private", "playlist-modify-public"} <= scopes
    assert "user-library-modify" not in scopes


def test_read_only_login_requests_no_write_scopes(settings):
    import dataclasses

    from playlist_curator.config import READ_SCOPES

    ro = dataclasses.replace(settings, scopes=READ_SCOPES)
    q = parse_qs(urlparse(build_authorize_url(ro, "c", "s")).query)
    assert not any("modify" in s for s in q["scope"][0].split())


def test_callback_parsing_validates_state_and_errors():
    assert parse_callback_url("http://127.0.0.1:8888/callback?code=abc&state=s1", "s1") == "abc"
    with pytest.raises(AuthError, match="state"):
        parse_callback_url("http://127.0.0.1:8888/callback?code=abc&state=evil", "s1")
    with pytest.raises(AuthError, match="access_denied"):
        parse_callback_url("http://127.0.0.1:8888/callback?error=access_denied&state=s1", "s1")


def test_settings_reject_localhost_and_missing_client_id(tmp_path):
    with pytest.raises(ConfigError, match="localhost"):
        Settings(client_id="x", redirect_uri="http://localhost:8888/callback").validate()
    with pytest.raises(ConfigError, match="SPOTIFY_CLIENT_ID"):
        Settings(client_id="").validate()
    with pytest.raises(ConfigError, match="loopback"):
        Settings(client_id="x", redirect_uri="http://example.com/cb").validate()


def test_login_exchanges_code_with_verifier_and_saves_tokens(settings):
    seen: list[dict] = []
    http = token_endpoint(
        [
            (
                200,
                {
                    "access_token": "A",
                    "refresh_token": "R",
                    "expires_in": 3600,
                    "scope": " ".join(settings.scopes),
                    "token_type": "Bearer",
                },
            )
        ],
        seen,
    )
    captured = {}

    def fake_browser(url: str) -> bool:
        captured["state"] = parse_qs(urlparse(url).query)["state"][0]
        captured["challenge"] = parse_qs(urlparse(url).query)["code_challenge"][0]
        return True

    def waiter(redirect_uri: str) -> str:
        # Runs on a thread after the browser call in real use; poll until state is known.
        import time

        for _ in range(100):
            if "state" in captured:
                break
            time.sleep(0.01)
        return f"{redirect_uri}?code=the-code&state={captured['state']}"

    tokens = login(
        settings,
        http=http,
        open_browser=fake_browser,
        callback_waiter=waiter,
        print_fn=lambda _: None,
    )
    assert tokens.access_token == "A"
    sent = seen[0]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-code"
    assert code_challenge_s256(sent["code_verifier"]) == captured["challenge"]
    assert "client_secret" not in sent

    stored = TokenStore(settings.token_path).load()
    assert stored is not None and stored.refresh_token == "R"
    if os.name == "posix":
        assert stat.S_IMODE(settings.token_path.stat().st_mode) == 0o600


def test_manual_login_uses_pasted_url(settings):
    seen: list[dict] = []
    http = token_endpoint(
        [(200, {"access_token": "A", "refresh_token": "R", "expires_in": 60})], seen
    )
    printed: list[str] = []

    def paste(_prompt: str) -> str:
        url = next(p for p in printed if p.startswith(auth.AUTHORIZE_URL))
        state = parse_qs(urlparse(url.strip()).query)["state"][0]
        return f"http://127.0.0.1:8888/callback?code=c2&state={state}"

    login(settings, manual=True, http=http, input_fn=paste, print_fn=printed.append)
    assert seen[0]["code"] == "c2"


def test_refresh_keeps_old_refresh_token_when_not_rotated(settings):
    store = TokenStore(settings.token_path)
    store.save(TokenSet("old", "R1", expires_at=0))  # already expired
    seen: list[dict] = []
    http = token_endpoint([(200, {"access_token": "new", "expires_in": 3600})], seen)
    mgr = TokenManager(settings, http=http)
    assert mgr.access_token() == "new"
    assert seen[0] == {
        "grant_type": "refresh_token",
        "refresh_token": "R1",
        "client_id": "client-123",
    }
    assert store.load().refresh_token == "R1"


def test_refresh_uses_rotated_refresh_token(settings):
    TokenStore(settings.token_path).save(TokenSet("old", "R1", expires_at=0))
    http = token_endpoint(
        [(200, {"access_token": "new", "refresh_token": "R2", "expires_in": 3600})], []
    )
    TokenManager(settings, http=http).access_token()
    assert TokenStore(settings.token_path).load().refresh_token == "R2"


def test_revoked_refresh_token_gives_actionable_error(settings):
    TokenStore(settings.token_path).save(TokenSet("old", "R1", expires_at=0))
    http = token_endpoint(
        [(400, {"error": "invalid_grant", "error_description": "Refresh token revoked"})], []
    )
    with pytest.raises(AuthError, match="curator auth login"):
        TokenManager(settings, http=http).access_token()


def test_no_tokens_means_not_logged_in(settings):
    with pytest.raises(NotLoggedInError):
        TokenManager(settings, http=httpx.Client()).access_token()


def test_corrupt_token_file_is_ignored(settings):
    settings.token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.token_path.write_text("{not json")
    assert TokenStore(settings.token_path).load() is None
    settings.token_path.write_text(json.dumps({"unexpected": 1}))
    assert TokenStore(settings.token_path).load() is None


def test_local_callback_server_captures_redirect():
    import socket
    import threading

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    result: dict[str, str] = {}
    t = threading.Thread(
        target=lambda: result.update(url=auth.wait_for_callback(redirect, timeout=10))
    )
    t.start()
    for _ in range(50):
        try:
            r = httpx.get(f"{redirect}?code=xyz&state=s", trust_env=False, timeout=2)
            break
        except httpx.ConnectError:
            import time

            time.sleep(0.05)
    t.join(timeout=10)
    assert r.status_code == 200
    assert parse_callback_url(result["url"], "s") == "xyz"
