"""OAuth 2.0 Authorization Code with PKCE for Spotify.

PKCE means no client secret is stored anywhere. Tokens are cached in
``<data_dir>/.tokens.json`` with owner-only permissions; that directory is git-ignored.

Reference: https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from playlist_curator.config import Settings

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
_VERIFIER_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


class AuthError(RuntimeError):
    pass


class NotLoggedInError(AuthError):
    def __init__(self) -> None:
        super().__init__("Not logged in to Spotify. Run: curator auth login")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float
    scope: str = ""
    token_type: str = "Bearer"

    def is_expired(self, skew_seconds: float = 60.0, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at - skew_seconds

    @property
    def scopes(self) -> set[str]:
        return set(self.scope.split())

    @classmethod
    def from_response(
        cls, payload: dict, previous_refresh_token: str | None = None, now: float | None = None
    ) -> TokenSet:
        if "access_token" not in payload:
            raise AuthError(f"Token response missing access_token: {sorted(payload)}")
        return cls(
            access_token=payload["access_token"],
            # Spotify may omit refresh_token on refresh; keep using the previous one.
            refresh_token=payload.get("refresh_token") or previous_refresh_token,
            expires_at=(now if now is not None else time.time())
            + float(payload.get("expires_in", 3600)),
            scope=payload.get("scope", ""),
            token_type=payload.get("token_type", "Bearer"),
        )


class TokenStore:
    """JSON token cache with owner-only file permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> TokenSet | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return TokenSet(**data)
        except (ValueError, TypeError) as exc:
            log.warning("Ignoring unreadable token cache %s: %s", self.path, exc)
            return None

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # Create with 0600 before writing secrets into it.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(tokens), fh)
        os.replace(tmp, self.path)

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False


# ---------------------------------------------------------------------------
# PKCE primitives
# ---------------------------------------------------------------------------


def generate_code_verifier(length: int = 64) -> str:
    if not 43 <= length <= 128:
        raise ValueError("PKCE code verifier must be 43-128 characters")
    return "".join(secrets.choice(_VERIFIER_ALPHABET) for _ in range(length))


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorize_url(settings: Settings, code_challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "state": state,
        "scope": " ".join(settings.scopes),
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def parse_callback_url(url: str, expected_state: str) -> str:
    """Extract the authorization code from a redirect URL, validating ``state``."""
    query = parse_qs(urlparse(url).query)
    state = (query.get("state") or [None])[0]
    if state != expected_state:
        raise AuthError("OAuth state mismatch; possible stale or forged callback. Try again.")
    if "error" in query:
        raise AuthError(f"Spotify authorization was not granted: {query['error'][0]}")
    code = (query.get("code") or [None])[0]
    if not code:
        raise AuthError("Callback URL did not contain an authorization code.")
    return code


def _token_request(http: httpx.Client, data: dict[str, str]) -> dict:
    resp = http.post(
        TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    if resp.status_code != 200:
        try:
            body = resp.json()
            detail = f"{body.get('error')}: {body.get('error_description', '')}"
        except ValueError:
            detail = resp.text[:200]
        raise AuthError(f"Token request failed ({resp.status_code}): {detail}")
    return resp.json()


def exchange_code(http: httpx.Client, settings: Settings, code: str, verifier: str) -> TokenSet:
    payload = _token_request(
        http,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.redirect_uri,
            "client_id": settings.client_id,
            "code_verifier": verifier,
        },
    )
    return TokenSet.from_response(payload)


def refresh_tokens(http: httpx.Client, client_id: str, tokens: TokenSet) -> TokenSet:
    if not tokens.refresh_token:
        raise NotLoggedInError()
    payload = _token_request(
        http,
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": client_id,
        },
    )
    return TokenSet.from_response(payload, previous_refresh_token=tokens.refresh_token)


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------


def wait_for_callback(redirect_uri: str, timeout: float = 300.0) -> str:
    """Serve one request on the loopback redirect URI and return the full callback URL."""
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    expected_path = parsed.path or "/"
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlparse(self.path).path != expected_path:
                self.send_response(404)
                self.end_headers()
                return
            captured["url"] = f"{parsed.scheme}://{parsed.netloc}{self.path}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif'><h2>Playlist Curator</h2>"
                b"<p>Authorization received. You can close this tab and return to the "
                b"terminal.</p></body></html>"
            )

        def log_message(self, *args: object) -> None:  # silence stdlib access log
            return

    try:
        server = HTTPServer((host, port), Handler)
    except OSError as exc:
        raise AuthError(
            f"Could not listen on {host}:{port} for the OAuth callback ({exc}). "
            "Is another login running, or is the port in use? Use --manual instead."
        ) from exc
    server.timeout = 1.0
    deadline = time.monotonic() + timeout
    try:
        while "url" not in captured and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "url" not in captured:
        raise AuthError("Timed out waiting for the Spotify authorization callback.")
    return captured["url"]


# ---------------------------------------------------------------------------
# High-level flows
# ---------------------------------------------------------------------------


def login(
    settings: Settings,
    *,
    manual: bool = False,
    http: httpx.Client | None = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    open_browser: Callable[[str], bool] = webbrowser.open,
    callback_waiter: Callable[[str], str] | None = None,
) -> TokenSet:
    """Run the interactive PKCE login and persist the tokens.

    ``manual=True`` skips the local server: you paste the URL your browser was redirected
    to (useful for WSL, SSH, or when the port is unavailable).
    """
    settings.validate()
    verifier = generate_code_verifier()
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(settings, code_challenge_s256(verifier), state)

    print_fn(f"Requesting scopes: {' '.join(settings.scopes)}")
    print_fn("Open this URL to authorize Playlist Curator:\n")
    print_fn(url + "\n")

    if manual:
        callback_url = input_fn("After approving, paste the full URL you were redirected to: ")
    else:
        waiter = callback_waiter or wait_for_callback
        result: dict[str, object] = {}

        def run() -> None:
            try:
                result["url"] = waiter(settings.redirect_uri)
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        with contextlib.suppress(Exception):  # no browser available: user opens the URL
            open_browser(url)
        print_fn(f"Waiting for the redirect to {settings.redirect_uri} ...")
        thread.join()
        if "error" in result:
            raise result["error"]  # type: ignore[misc]
        callback_url = str(result["url"])

    code = parse_callback_url(callback_url.strip(), state)
    owns_http = http is None
    http = http or httpx.Client(timeout=30.0)
    try:
        tokens = exchange_code(http, settings, code, verifier)
    finally:
        if owns_http:
            http.close()
    TokenStore(settings.token_path).save(tokens)
    missing = set(settings.scopes) - tokens.scopes
    if tokens.scope and missing:
        log.warning("Spotify did not grant these scopes: %s", ", ".join(sorted(missing)))
    return tokens


class TokenManager:
    """Supplies a valid access token, refreshing and persisting as needed."""

    def __init__(
        self, settings: Settings, http: httpx.Client | None = None, store: TokenStore | None = None
    ) -> None:
        self.settings = settings
        self.store = store or TokenStore(settings.token_path)
        self._http = http or httpx.Client(timeout=30.0)
        self._tokens = self.store.load()
        self._lock = threading.Lock()

    @property
    def tokens(self) -> TokenSet | None:
        return self._tokens

    def access_token(self) -> str:
        with self._lock:
            if self._tokens is None:
                raise NotLoggedInError()
            if self._tokens.is_expired():
                self._refresh_locked()
            return self._tokens.access_token

    def force_refresh(self) -> str:
        with self._lock:
            self._refresh_locked()
            assert self._tokens is not None
            return self._tokens.access_token

    def _refresh_locked(self) -> None:
        if self._tokens is None:
            raise NotLoggedInError()
        log.debug("Refreshing Spotify access token")
        try:
            self._tokens = refresh_tokens(self._http, self.settings.client_id, self._tokens)
        except AuthError as exc:
            if "invalid_grant" in str(exc):
                raise AuthError(
                    "Spotify rejected the saved refresh token (revoked or expired). "
                    "Run: curator auth login"
                ) from exc
            raise
        self.store.save(self._tokens)
