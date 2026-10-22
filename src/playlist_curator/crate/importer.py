"""Import Liked Songs and playlists from Spotify into the local database.

Two phases so a failure never leaves half-written state:
1. **Fetch** everything from Spotify into memory (read-only API calls).
2. **Write** it in a single SQLite transaction.

Playlists whose ``snapshot_id`` hasn't changed since the last successful fetch are not
re-downloaded (Spotify's own recommendation for staying under rate limits).

Since February 2026, playlist *contents* are only returned for playlists you own or
collaborate on. Followed playlists made by others are recorded with
``contents_status='unavailable'`` and contribute nothing to the crate. That's an API limit,
not a bug; the inventory reports how many playlists it affects.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from playlist_curator.crate.deduplication import rebuild as rebuild_recordings
from playlist_curator.models import (
    LIKED_SONGS_SOURCE_ID,
    ImportCounts,
    ParsedEntry,
    PlaylistSummary,
    SourceEntry,
    SourceKind,
    Track,
    parse_playlist_item,
    parse_playlist_summary,
    parse_saved_track,
    parse_user,
)
from playlist_curator.spotify.client import RateLimitedError, SpotifyAPIError, SpotifyClient
from playlist_curator.storage.database import Database, utcnow

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


class AccountMismatchError(RuntimeError):
    pass


@dataclass
class ImportResult:
    run_id: int
    status: str  # ok | partial | failed
    counts: ImportCounts
    errors: list[dict[str, Any]] = field(default_factory=list)
    user_id: str | None = None


@dataclass
class _PlaylistFetch:
    summary: PlaylistSummary
    owned: bool
    status: str  # fetched | unchanged | unavailable | error
    note: str | None = None
    entries: list[SourceEntry] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)


def _collect(
    raw_items: Any,
    parse: Callable[[dict], ParsedEntry],
    kind: SourceKind,
    source_id: str,
    skipped: Counter[str],
) -> tuple[list[Track], list[SourceEntry]]:
    tracks: list[Track] = []
    entries: list[SourceEntry] = []
    for position, raw in enumerate(raw_items):
        parsed = parse(raw or {})
        if parsed.track is None:
            skipped[str(parsed.skip_reason or "unknown")] += 1
            continue
        tracks.append(parsed.track)
        entries.append(
            SourceEntry(
                track_uri=parsed.track.uri,
                kind=kind,
                source_id=source_id,
                position=position,  # raw position, so gaps show where entries were skipped
                added_at=parsed.added_at,
                added_by=parsed.added_by,
            )
        )
    return tracks, entries


def import_library(
    client: SpotifyClient,
    db: Database,
    *,
    force: bool = False,
    progress: Progress | None = None,
) -> ImportResult:
    say = progress or (lambda msg: log.info(msg))
    run_id = db.start_run()
    counts = ImportCounts()
    errors: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()

    try:
        me = parse_user(client.me())
        stored_user = db.get_meta("user_id")
        if stored_user and stored_user != me.spotify_id:
            raise AccountMismatchError(
                f"This database belongs to Spotify user '{stored_user}' but you are logged in "
                f"as '{me.spotify_id}'. Run 'curator purge' first or use another data dir."
            )
        say(f"Logged in as {me.display_name or me.spotify_id}")

        # ---- fetch: Liked Songs ------------------------------------------------
        say("Reading Liked Songs ...")
        liked_tracks, liked_entries = _collect(
            client.iter_saved_tracks(),
            parse_saved_track,
            SourceKind.LIKED,
            LIKED_SONGS_SOURCE_ID,
            skipped,
        )
        counts.liked_entries = len(liked_entries)
        say(f"  {len(liked_entries)} liked songs")

        # ---- fetch: playlists --------------------------------------------------
        say("Reading playlists ...")
        summaries: dict[str, PlaylistSummary] = {}
        # Playlists the app created (Phase 6) are output, not source material: skip them
        managed_ids = {r[0] for r in db.conn.execute("SELECT spotify_id FROM managed_playlists")}
        # ... including ones from before a start-over, in case removing them failed
        managed_ids |= set(json.loads(db.get_meta("retired_playlist_ids") or "[]"))
        for raw in client.iter_my_playlists():
            if raw and raw.get("id") and raw["id"] not in managed_ids:
                summaries.setdefault(raw["id"], parse_playlist_summary(raw))
        counts.playlists_seen = len(summaries)

        fetches: list[_PlaylistFetch] = []
        for i, summary in enumerate(summaries.values(), start=1):
            owned = summary.owned_by(me.spotify_id)
            prev = db.get_playlist_row(summary.spotify_id)
            unchanged = (
                not force
                and prev is not None
                and summary.snapshot_id is not None
                and prev["fetched_snapshot_id"] == summary.snapshot_id
                and prev["contents_status"] in ("fetched", "unavailable")
            )
            if unchanged:
                assert prev is not None
                fetches.append(_PlaylistFetch(summary, owned, "unchanged", prev["contents_note"]))
                counts.playlists_unchanged += 1
                continue

            say(f"  [{i}/{len(summaries)}] {summary.name}")
            try:
                tracks, entries = _collect(
                    client.iter_playlist_items(summary.spotify_id),
                    parse_playlist_item,
                    SourceKind.PLAYLIST,
                    summary.spotify_id,
                    skipped,
                )
                fetches.append(_PlaylistFetch(summary, owned, "fetched", None, entries, tracks))
                counts.playlists_fetched += 1
                counts.playlist_entries += len(entries)
            except RateLimitedError:
                raise
            except SpotifyAPIError as exc:
                if exc.status in (403, 404):
                    note = (
                        "Spotify only returns contents for playlists you own or collaborate on."
                        if exc.status == 403
                        else "Playlist not found (deleted or no longer accessible)."
                    )
                    fetches.append(_PlaylistFetch(summary, owned, "unavailable", note))
                    counts.playlists_contents_unavailable += 1
                else:
                    log.error("Failed to read playlist %s: %s", summary.name, exc)
                    errors.append(
                        {"playlist_id": summary.spotify_id, "name": summary.name, "error": str(exc)}
                    )
                    fetches.append(_PlaylistFetch(summary, owned, "error", str(exc)))

        # ---- write -------------------------------------------------------------
        say("Saving ...")
        now = utcnow()
        before = db.referenced_track_uris()
        with db.transaction():
            db.set_meta("user_id", me.spotify_id)
            all_tracks = list(liked_tracks)
            for f in fetches:
                all_tracks.extend(f.tracks)
            db.upsert_tracks(_unique_by_uri(all_tracks), seen_at=now)
            db.replace_source_entries(SourceKind.LIKED, LIKED_SONGS_SOURCE_ID, liked_entries)

            for f in fetches:
                prev = db.get_playlist_row(f.summary.spotify_id)
                if f.status == "fetched":
                    db.replace_source_entries(SourceKind.PLAYLIST, f.summary.spotify_id, f.entries)
                    status, fetched_snapshot, fetched_at = "fetched", f.summary.snapshot_id, now
                elif f.status == "unavailable":
                    db.replace_source_entries(SourceKind.PLAYLIST, f.summary.spotify_id, [])
                    status, fetched_snapshot, fetched_at = "unavailable", f.summary.snapshot_id, now
                elif f.status == "unchanged":
                    assert prev is not None
                    status = prev["contents_status"]
                    fetched_snapshot, fetched_at = (
                        prev["fetched_snapshot_id"],
                        prev["last_fetched_at"],
                    )
                else:  # error: keep whatever we had, and force a retry next time
                    status = "error"
                    fetched_snapshot = None
                    fetched_at = prev["last_fetched_at"] if prev else None
                db.upsert_playlist(
                    f.summary,
                    owned_by_user=f.owned,
                    contents_status=status,
                    contents_note=f.note,
                    fetched_snapshot_id=fetched_snapshot,
                    last_fetched_at=fetched_at,
                )

            gone = db.delete_playlists_not_in(set(summaries))
            if gone:
                say(f"  {len(gone)} playlist(s) no longer in your library; forgotten locally")
            db.delete_orphan_tracks()
        after = db.referenced_track_uris()
        counts.new_tracks = len(after - before)
        counts.removed_tracks = len(before - after)
        counts.skipped = dict(skipped)

        say("Grouping duplicate recordings ...")
        dedup = rebuild_recordings(db)
        counts.recordings = len(dedup.recordings)
        counts.pending_duplicate_suggestions = len(dedup.suggestions)

        status = "partial" if errors else "ok"
        db.finish_run(run_id, status, counts, errors)
        return ImportResult(run_id, status, counts, errors, me.spotify_id)

    except Exception as exc:
        errors.append({"error": f"{type(exc).__name__}: {exc}"})
        counts.skipped = dict(skipped)
        db.finish_run(run_id, "failed", counts, errors)
        raise


def _unique_by_uri(tracks: list[Track]) -> list[Track]:
    """Keep the most complete copy of each URI (e.g. one that has an ISRC)."""
    best: dict[str, Track] = {}
    for t in tracks:
        cur = best.get(t.uri)
        if cur is None or (cur.isrc is None and t.isrc is not None):
            best[t.uri] = t
    return list(best.values())
