"""Application configuration, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# Read-only scopes are all Phase 1 needs. Write scopes are added when playlist creation
# lands (Phase 6) and will require logging in again so you explicitly grant them.
READ_SCOPES: tuple[str, ...] = (
    "user-library-read",  # GET /me/tracks (Liked Songs)
    "playlist-read-private",  # GET /me/playlists, GET /playlists/{id}/items for private lists
    "playlist-read-collaborative",  # collaborative playlists you are a member of
)
WRITE_SCOPES: tuple[str, ...] = (
    "playlist-modify-private",
    "playlist-modify-public",
)

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
DEFAULT_MB_CONTACT = "https://github.com/shephmic77/spotify-playlist-curator"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    client_id: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    data_dir: Path = field(default_factory=lambda: Path("data"))
    scopes: tuple[str, ...] = READ_SCOPES
    log_level: str = "INFO"
    lastfm_api_key: str = ""
    # MusicBrainz requires a contact (URL or email) in the User-Agent of every request.
    musicbrainz_contact: str = DEFAULT_MB_CONTACT

    @property
    def db_path(self) -> Path:
        return self.data_dir / "curator.sqlite3"

    @property
    def token_path(self) -> Path:
        return self.data_dir / ".tokens.json"

    def validate(self) -> None:
        if not self.client_id:
            raise ConfigError(
                "SPOTIFY_CLIENT_ID is not set. Copy .env.example to .env and add the Client ID "
                "from your app in the Spotify Developer Dashboard."
            )
        parsed = urlparse(self.redirect_uri)
        if parsed.hostname == "localhost":
            raise ConfigError(
                "Spotify no longer accepts 'localhost' redirect URIs. Use "
                f"{DEFAULT_REDIRECT_URI} (and register exactly that URI in the dashboard)."
            )
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1"):
            raise ConfigError("Plain http redirect URIs are only allowed for loopback addresses.")


def load_settings(env_file: str | os.PathLike[str] | None = ".env") -> Settings:
    """Build Settings from the environment. A .env file is loaded if present."""
    if env_file is not None and Path(env_file).exists():
        load_dotenv(env_file, override=False)
    return Settings(
        client_id=os.environ.get("SPOTIFY_CLIENT_ID", "").strip(),
        redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip(),
        data_dir=Path(os.environ.get("CURATOR_DATA_DIR", "data")).expanduser(),
        log_level=os.environ.get("CURATOR_LOG_LEVEL", "INFO").upper(),
        lastfm_api_key=os.environ.get("LASTFM_API_KEY", "").strip(),
        musicbrainz_contact=os.environ.get("MUSICBRAINZ_CONTACT", DEFAULT_MB_CONTACT).strip()
        or DEFAULT_MB_CONTACT,
    )


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO, which would print URLs on every page fetch.
    logging.getLogger("httpx").setLevel(logging.WARNING)
