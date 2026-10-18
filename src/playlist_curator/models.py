"""Domain models and parsers that normalize raw Spotify Web API JSON.

Parsers are defensive on purpose: the February 2026 migration renamed several fields
(``track`` -> ``item`` on playlist entries, ``tracks`` -> ``items`` on playlist objects)
and Extended-Quota apps still see the old shape. We accept both so a schema change
on either side degrades to a logged skip instead of a crash.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import unquote_plus

from pydantic import BaseModel, ConfigDict, Field

LIKED_SONGS_SOURCE_ID = "__liked__"


class SourceKind(StrEnum):
    LIKED = "liked"
    PLAYLIST = "playlist"


class SkipReason(StrEnum):
    """Why a raw entry did not become a Track. Counted and reported, never silently dropped."""

    UNAVAILABLE = "unavailable"  # entry exists but the API returned no track object
    EPISODE = "episode"  # podcast episode, out of scope for music curation
    NO_URI = "no_uri"  # malformed object we cannot identify


class ArtistRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    spotify_id: str | None = None


class Track(BaseModel):
    """One Spotify catalog track or one local-file entry, keyed by its URI.

    Phase 1 keeps one row per URI. Grouping different URIs that are the same recording
    (relinked IDs, single vs. album copies) is Phase 2 deduplication and is deliberately
    not done here, so no information is lost before that logic exists.
    """

    model_config = ConfigDict(frozen=True)

    uri: str
    spotify_id: str | None = None
    name: str
    artists: tuple[ArtistRef, ...] = ()
    album_name: str | None = None
    album_id: str | None = None
    album_type: str | None = None
    release_date: str | None = None
    release_date_precision: str | None = None
    duration_ms: int | None = None
    explicit: bool | None = None
    isrc: str | None = None
    is_local: bool = False
    is_playable: bool | None = None
    disc_number: int | None = None
    track_number: int | None = None

    @property
    def key(self) -> str:
        return self.uri

    @property
    def artist_names(self) -> str:
        return ", ".join(a.name for a in self.artists)

    @property
    def release_year(self) -> int | None:
        if self.release_date and self.release_date[:4].isdigit():
            year = int(self.release_date[:4])
            # Spotify uses 0000 for some local/unknown dates.
            return year if year > 0 else None
        return None


class PlaylistSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    spotify_id: str
    name: str
    description: str | None = None
    owner_id: str | None = None
    owner_name: str | None = None
    collaborative: bool = False
    public: bool | None = None
    snapshot_id: str | None = None
    total_items: int | None = None

    def owned_by(self, user_id: str) -> bool:
        return self.owner_id is not None and self.owner_id == user_id


class SourceEntry(BaseModel):
    """One occurrence of a track in a source (Liked Songs or a playlist)."""

    model_config = ConfigDict(frozen=True)

    track_uri: str
    kind: SourceKind
    source_id: str
    position: int
    added_at: datetime | None = None
    added_by: str | None = None


class ParsedEntry(BaseModel):
    track: Track | None = None
    added_at: datetime | None = None
    added_by: str | None = None
    skip_reason: SkipReason | None = None


class UserProfile(BaseModel):
    spotify_id: str
    display_name: str | None = None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_local_uri(uri: str) -> dict[str, Any]:
    """Split a local-file URI into its parts.

    Observed format (not formally documented by Spotify):
    ``spotify:local:{artist}:{album}:{title}:{duration_seconds}`` with each field
    URL-encoded using ``+`` for spaces. Any field may be empty. Returns only the
    fields that were present; never invents values.
    """
    prefix = "spotify:local:"
    if not uri.startswith(prefix):
        return {}
    parts = uri[len(prefix) :].split(":")
    if len(parts) < 4:
        return {}
    # Titles cannot contain a raw ':' (it would be encoded), but be safe: the last part is
    # the duration, the first two are artist/album, everything between is the title.
    artist, album, *title_parts, seconds = parts
    out: dict[str, Any] = {}
    if artist:
        out["artist"] = unquote_plus(artist)
    if album:
        out["album"] = unquote_plus(album)
    title = unquote_plus(":".join(title_parts))
    if title:
        out["title"] = title
    if seconds.isdigit():
        out["duration_ms"] = int(seconds) * 1000
    return out


def parse_track(obj: dict[str, Any] | None, *, is_local_hint: bool = False) -> ParsedEntry:
    """Normalize a TrackObject (or local-file pseudo track)."""
    if obj is None:
        return ParsedEntry(skip_reason=SkipReason.UNAVAILABLE)
    obj_type = obj.get("type", "track")
    if obj_type == "episode" or obj.get("episode") is True:
        return ParsedEntry(skip_reason=SkipReason.EPISODE)
    uri = obj.get("uri")
    if not uri:
        return ParsedEntry(skip_reason=SkipReason.NO_URI)

    is_local = bool(obj.get("is_local") or is_local_hint or uri.startswith("spotify:local:"))
    album = obj.get("album") or {}
    artists = tuple(
        ArtistRef(name=a.get("name") or "", spotify_id=a.get("id"))
        for a in (obj.get("artists") or [])
        if a and (a.get("name") or a.get("id"))
    )
    name = obj.get("name") or ""
    album_name = album.get("name") or None
    duration_ms = obj.get("duration_ms")

    if is_local:
        # Fill gaps from the URI, but only with what the URI actually contains.
        from_uri = parse_local_uri(uri)
        if not name:
            name = from_uri.get("title", "")
        if not artists and "artist" in from_uri:
            artists = (ArtistRef(name=from_uri["artist"]),)
        if not album_name:
            album_name = from_uri.get("album")
        if not duration_ms:
            duration_ms = from_uri.get("duration_ms")

    external_ids = obj.get("external_ids") or {}
    track = Track(
        uri=uri,
        spotify_id=None if is_local else obj.get("id"),
        name=name,
        artists=artists,
        album_name=album_name,
        album_id=None if is_local else album.get("id"),
        album_type=album.get("album_type"),
        release_date=album.get("release_date") or None,
        release_date_precision=album.get("release_date_precision"),
        duration_ms=duration_ms or None,
        explicit=obj.get("explicit"),
        isrc=(external_ids.get("isrc") or None) if not is_local else None,
        is_local=is_local,
        is_playable=obj.get("is_playable"),
        disc_number=obj.get("disc_number"),
        track_number=obj.get("track_number"),
    )
    return ParsedEntry(track=track)


def parse_saved_track(obj: dict[str, Any]) -> ParsedEntry:
    """Normalize a SavedTrackObject from GET /me/tracks."""
    inner = obj.get("track") if "track" in obj else obj.get("item")
    parsed = parse_track(inner)
    return parsed.model_copy(update={"added_at": _parse_datetime(obj.get("added_at"))})


def parse_playlist_item(obj: dict[str, Any]) -> ParsedEntry:
    """Normalize a PlaylistTrackObject. Prefers ``item``; falls back to deprecated ``track``."""
    inner = obj.get("item") if obj.get("item") is not None else obj.get("track")
    parsed = parse_track(inner, is_local_hint=bool(obj.get("is_local")))
    added_by = (obj.get("added_by") or {}).get("id")
    return parsed.model_copy(
        update={"added_at": _parse_datetime(obj.get("added_at")), "added_by": added_by}
    )


def parse_playlist_summary(obj: dict[str, Any]) -> PlaylistSummary:
    owner = obj.get("owner") or {}
    # Renamed from "tracks" to "items" in the Feb 2026 migration.
    ref = obj.get("items") if isinstance(obj.get("items"), dict) else obj.get("tracks")
    total = ref.get("total") if isinstance(ref, dict) else None
    return PlaylistSummary(
        spotify_id=obj["id"],
        name=obj.get("name") or "(untitled)",
        description=obj.get("description") or None,
        owner_id=owner.get("id"),
        owner_name=owner.get("display_name"),
        collaborative=bool(obj.get("collaborative")),
        public=obj.get("public"),
        snapshot_id=obj.get("snapshot_id"),
        total_items=total,
    )


def parse_user(obj: dict[str, Any]) -> UserProfile:
    return UserProfile(spotify_id=obj["id"], display_name=obj.get("display_name"))


class ImportCounts(BaseModel):
    liked_entries: int = 0
    playlists_seen: int = 0
    playlists_fetched: int = 0
    playlists_unchanged: int = 0
    playlists_contents_unavailable: int = 0
    playlist_entries: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    new_tracks: int = 0
    removed_tracks: int = 0
