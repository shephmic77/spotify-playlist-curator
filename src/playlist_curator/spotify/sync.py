"""Phase 6: create and update your approved playlists in Spotify.

Safety rules enforced here (and tested):
- The app writes **only** to playlists in ``managed_playlists``, i.e. ones it created. Your
  old playlists, followed playlists and Liked Songs are never written to.
- ``plan`` never writes. ``apply`` writes only what the plan shows, and only when you ask.
- New playlists are **private**.
- Minimal changes: only the songs that need adding or removing are touched; nothing is
  re-added if any copy of the same recording is already there; order is left alone.
- Before every change the current state is saved (``playlist_snapshots``) so it can be
  rolled back. After writing, the playlist is re-read to confirm the result.
- Your own edits in Spotify win: a song you added to a managed playlist since the app's last
  write is kept and recorded as a "keep" decision; a song you removed is recorded as a
  "reject" and isn't re-added.
- Local files can't be added through the API; they're listed as manual actions instead.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from playlist_curator.curator import feedback as fb
from playlist_curator.curator.identities import Identity, list_identities
from playlist_curator.curator.scoring import latest_run
from playlist_curator.models import parse_playlist_item
from playlist_curator.spotify.client import SpotifyAPIError, SpotifyClient
from playlist_curator.storage.database import Database, utcnow

log = logging.getLogger(__name__)

DESCRIPTION_SUFFIX = "Made with Playlist Curator."
MAX_DESCRIPTION = 300


class SyncError(RuntimeError):
    pass


class NotManaged(SyncError):
    pass


@dataclass
class PlaylistPlan:
    identity_id: int
    name: str
    action: str  # create | update | unchanged | skip
    spotify_id: str | None
    add: list[str] = field(default_factory=list)  # URIs, in draft order
    remove: list[str] = field(default_factory=list)
    keep_manual_adds: list[str] = field(default_factory=list)  # you added these in Spotify
    respect_manual_removes: list[str] = field(default_factory=list)  # you removed these
    local_files: list[str] = field(default_factory=list)  # labels, for manual action
    rename: str | None = None
    new_description: str | None = None
    current_count: int = 0
    target_count: int = 0
    note: str = ""


@dataclass
class SyncPlan:
    draft_run: int | None
    playlists: list[PlaylistPlan]

    @property
    def writes(self) -> int:
        return sum(1 for p in self.playlists if p.action in ("create", "update"))


@dataclass
class SyncResult:
    run_id: int
    status: str
    applied: list[dict[str, Any]]
    errors: list[dict[str, str]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def description_for(ident: Identity) -> str:
    base = (ident.concept or ident.name).strip()
    text = f"{base} · {DESCRIPTION_SUFFIX}" if base else DESCRIPTION_SUFFIX
    return text[:MAX_DESCRIPTION]


def managed(db: Database) -> dict[int, dict[str, Any]]:
    return {
        r["identity_id"]: dict(r)
        for r in db.conn.execute("SELECT * FROM managed_playlists WHERE status != 'released'")
    }


def managed_spotify_ids(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT spotify_id FROM managed_playlists")}


def _rec_of_uri(db: Database) -> dict[str, str]:
    return {r[0]: r[1] for r in db.conn.execute("SELECT uri, recording_id FROM recording_members")}


def _label(db: Database, uri: str) -> str:
    row = db.conn.execute("SELECT name, artists_json FROM tracks WHERE uri=?", (uri,)).fetchone()
    if not row:
        return uri
    artists = ", ".join(a["name"] for a in json.loads(row["artists_json"]))
    return f"{row['name']} — {artists}" if artists else row["name"]


def read_playlist_uris(client: SpotifyClient, spotify_id: str) -> list[str]:
    uris = []
    for raw in client.iter_playlist_items(spotify_id):
        parsed = parse_playlist_item(raw or {})
        if parsed.track is not None:
            uris.append(parsed.track.uri)
    return uris


def _target(db: Database, ident: Identity, run: int) -> tuple[list[str], list[str]]:
    """(catalog URIs in draft order, local-file URIs) for one playlist's latest draft."""
    rows = db.conn.execute(
        """SELECT r.primary_uri FROM draft_members d
           JOIN recordings r ON r.recording_id = d.recording_id
           WHERE d.run_id=? AND d.identity_id=? AND d.decision='include'
           ORDER BY d.score DESC""",
        (run, ident.identity_id),
    ).fetchall()
    catalog, local = [], []
    for (uri,) in rows:
        (local if uri.startswith("spotify:local:") else catalog).append(uri)
    return catalog, local


# ---------------------------------------------------------------------------
# Plan (read-only)
# ---------------------------------------------------------------------------


def plan(db: Database, client: SpotifyClient | None, *, only: list[str] | None = None) -> SyncPlan:
    """Work out what a sync would change. Reads Spotify for managed playlists; never writes."""
    run = latest_run(db)
    if run is None:
        raise SyncError("No draft playlists yet. Run: curator draft")
    reg = managed(db)
    rec_of = _rec_of_uri(db)
    wanted = {o.casefold() for o in only} if only else None
    plans = []
    for ident in list_identities(db):
        if wanted and ident.name.casefold() not in wanted:
            continue
        target, local = _target(db, ident, run)
        pp = PlaylistPlan(
            ident.identity_id,
            ident.name,
            "skip",
            None,
            local_files=[_label(db, u) for u in local],
            target_count=len(target),
        )
        entry = reg.get(ident.identity_id)
        if entry is None:
            if not target:
                pp.note = "empty draft: nothing to create"
                plans.append(pp)
                continue
            pp.action, pp.add = "create", list(target)
            pp.new_description = description_for(ident)
            plans.append(pp)
            continue

        pp.spotify_id = entry["spotify_id"]
        if client is None:
            pp.note = "not checked (no Spotify connection)"
            plans.append(pp)
            continue
        try:
            current = read_playlist_uris(client, entry["spotify_id"])
        except SpotifyAPIError as exc:
            if exc.status == 404:
                pp.note = "deleted in Spotify; it will be re-created"
                pp.action, pp.spotify_id, pp.add = "create", None, list(target)
                pp.new_description = description_for(ident)
            else:
                pp.note = f"couldn't read it: {exc.message}"
            plans.append(pp)
            continue
        pp.current_count = len(current)
        last = set(json.loads(entry["last_uris_json"]))
        cur_set = set(current)
        # Your edits in Spotify since the app's last write
        pp.keep_manual_adds = [u for u in current if u not in last]
        pp.respect_manual_removes = [u for u in last if u not in cur_set]
        removed_recs = {rec_of.get(u, u) for u in pp.respect_manual_removes}
        kept_recs = {rec_of.get(u, u) for u in pp.keep_manual_adds}

        present_recs = {rec_of.get(u, u) for u in current}
        target_recs = {rec_of.get(u, u) for u in target}
        pp.add = [
            u
            for u in target
            if rec_of.get(u, u) not in present_recs and rec_of.get(u, u) not in removed_recs
        ]
        pp.remove = [
            u
            for u in dict.fromkeys(current)
            if rec_of.get(u, u) not in target_recs
            and rec_of.get(u, u) not in kept_recs
            and not u.startswith("spotify:local:")  # can't be managed via the API
        ]
        if ident.name != entry["name"]:
            pp.rename = ident.name
        desc = description_for(ident)
        if desc != (entry["description"] or ""):
            pp.new_description = desc
        changed = pp.add or pp.remove or pp.rename or pp.new_description
        # your own edits in Spotify still need recording as decisions, even with no writes
        manual = pp.keep_manual_adds or pp.respect_manual_removes
        pp.action = "update" if (changed or manual) else "unchanged"
        plans.append(pp)
    return SyncPlan(run, plans)


# ---------------------------------------------------------------------------
# Apply (writes)
# ---------------------------------------------------------------------------


def _snapshot(
    db: Database,
    identity_id: int,
    spotify_id: str,
    uris: list[str],
    reason: str,
    run_id: int | None,
) -> int:
    cur = db.conn.execute(
        "INSERT INTO playlist_snapshots (identity_id, spotify_id, taken_at, reason, uris_json,"
        " sync_run_id) VALUES (?,?,?,?,?,?)",
        (identity_id, spotify_id, utcnow(), reason, json.dumps(uris), run_id),
    )
    return int(cur.lastrowid or 0)


def _guard(db: Database, spotify_id: str) -> None:
    if spotify_id not in managed_spotify_ids(db):
        raise NotManaged(f"Refusing to write to {spotify_id}: the app didn't create it.")


def apply(
    db: Database,
    client: SpotifyClient,
    sync_plan: SyncPlan,
    *,
    progress: Callable[[str], None] | None = None,
) -> SyncResult:
    say = progress or (lambda _m: None)
    started = utcnow()
    cur = db.conn.execute(
        "INSERT INTO sync_runs (started_at, status) VALUES (?, 'running')", (started,)
    )
    run_id = int(cur.lastrowid or 0)
    applied, errors = [], []
    idents = {i.identity_id: i for i in list_identities(db)}

    for pp in sync_plan.playlists:
        if pp.action not in ("create", "update"):
            continue
        ident = idents.get(pp.identity_id)
        if ident is None:
            continue
        try:
            if pp.action == "create":
                say(f"Creating '{pp.name}' (private) with {len(pp.add)} songs ...")
                created = client.create_playlist(
                    pp.name, pp.new_description or description_for(ident), public=False
                )
                sid = created["id"]
                db.conn.execute(
                    "INSERT OR REPLACE INTO managed_playlists VALUES (?,?,?,?, 'active', ?, '[]',"
                    " ?, NULL)",
                    (
                        pp.identity_id,
                        sid,
                        pp.name,
                        pp.new_description or description_for(ident),
                        created.get("snapshot_id"),
                        utcnow(),
                    ),
                )
                _snapshot(db, pp.identity_id, sid, [], "before_sync", run_id)
                if pp.add:
                    client.add_items(sid, pp.add)
            else:
                sid = pp.spotify_id or ""
                _guard(db, sid)
                say(f"Updating '{pp.name}': +{len(pp.add)} / -{len(pp.remove)} ...")
                before = read_playlist_uris(client, sid)
                _snapshot(db, pp.identity_id, sid, before, "before_sync", run_id)
                # Your Spotify edits become decisions, so drafts keep them from now on
                for u in pp.keep_manual_adds:
                    if not u.startswith("spotify:local:"):
                        fb.decide_uri(db, ident, u, "keep")
                for u in pp.respect_manual_removes:
                    fb.decide_uri(db, ident, u, "reject")
                if pp.remove:
                    client.remove_items(sid, pp.remove)
                if pp.add:
                    client.add_items(sid, pp.add)
                if pp.rename or pp.new_description:
                    client.update_playlist_details(
                        sid, name=pp.rename, description=pp.new_description
                    )
            # Verify by reading back
            after = read_playlist_uris(client, sid)
            missing = [u for u in pp.add if u not in after]
            lingering = [u for u in pp.remove if u in after]
            if missing or lingering:
                raise SyncError(
                    f"verification failed: {len(missing)} not added, {len(lingering)} not removed"
                )
            _snapshot(db, pp.identity_id, sid, after, "after_sync", run_id)
            db.conn.execute(
                """UPDATE managed_playlists SET name=?, description=?, status='active',
                       last_uris_json=?, last_synced_at=? WHERE identity_id=?""",
                (
                    ident.name,
                    pp.new_description or description_for(ident),
                    json.dumps(after),
                    utcnow(),
                    pp.identity_id,
                ),
            )
            applied.append(
                {
                    "playlist": pp.name,
                    "action": pp.action,
                    "added": len(pp.add),
                    "removed": len(pp.remove),
                    "spotify_id": sid,
                }
            )
        except (SpotifyAPIError, SyncError, ValueError) as exc:
            log.error("Sync failed for %s: %s", pp.name, exc)
            errors.append({"playlist": pp.name, "error": str(exc)})
            say(f"  ! {pp.name}: {exc}")

    status = "ok" if not errors else ("partial" if applied else "failed")
    db.conn.execute(
        "UPDATE sync_runs SET finished_at=?, status=?, summary_json=?, errors_json=? "
        "WHERE run_id=?",
        (
            utcnow(),
            status,
            json.dumps({"applied": applied, "plan": [asdict(p) for p in sync_plan.playlists]}),
            json.dumps(errors),
            run_id,
        ),
    )
    return SyncResult(run_id, status, applied, errors)


# ---------------------------------------------------------------------------
# Rollback and history
# ---------------------------------------------------------------------------


def snapshots(db: Database, identity_id: int) -> list[dict[str, Any]]:
    return [
        {
            "snapshot_id": r["snapshot_id"],
            "taken_at": r["taken_at"],
            "reason": r["reason"],
            "songs": len(json.loads(r["uris_json"])),
        }
        for r in db.conn.execute(
            "SELECT * FROM playlist_snapshots WHERE identity_id=? ORDER BY snapshot_id DESC",
            (identity_id,),
        )
    ]


def rollback(
    db: Database, client: SpotifyClient, identity_id: int, snapshot_id: int | None = None
) -> dict[str, Any]:
    """Restore a managed playlist to a saved state (default: before the last sync)."""
    entry = managed(db).get(identity_id)
    if entry is None:
        raise NotManaged("This playlist isn't one the app manages in Spotify.")
    sid = entry["spotify_id"]
    _guard(db, sid)
    if snapshot_id is None:
        row = db.conn.execute(
            "SELECT * FROM playlist_snapshots WHERE identity_id=? AND reason='before_sync' "
            "ORDER BY snapshot_id DESC LIMIT 1",
            (identity_id,),
        ).fetchone()
    else:
        row = db.conn.execute(
            "SELECT * FROM playlist_snapshots WHERE identity_id=? AND snapshot_id=?",
            (identity_id, snapshot_id),
        ).fetchone()
    if row is None:
        raise SyncError("No saved state to roll back to.")
    target = [u for u in json.loads(row["uris_json"]) if not u.startswith("spotify:local:")]
    before = read_playlist_uris(client, sid)
    _snapshot(db, identity_id, sid, before, "before_rollback", None)
    if target:
        client.replace_items(sid, target)
    else:
        current = [u for u in before if not u.startswith("spotify:local:")]
        if current:
            client.remove_items(sid, list(dict.fromkeys(current)))
    after = read_playlist_uris(client, sid)
    _snapshot(db, identity_id, sid, after, "after_rollback", None)
    db.conn.execute(
        "UPDATE managed_playlists SET last_uris_json=?, last_synced_at=? WHERE identity_id=?",
        (json.dumps(after), utcnow(), identity_id),
    )
    return {"restored_snapshot": row["snapshot_id"], "songs": len(after)}


def release(db: Database, identity_id: int) -> None:
    """Stop managing a playlist. It stays in your Spotify account untouched."""
    db.conn.execute(
        "UPDATE managed_playlists SET status='released' WHERE identity_id=?", (identity_id,)
    )


def local_file_actions(db: Database) -> list[tuple[str, list[str]]]:
    """(playlist, local-file songs to add by hand in the desktop app)."""
    run = latest_run(db)
    if run is None:
        return []
    out = []
    for ident in list_identities(db):
        _, local = _target(db, ident, run)
        if local:
            out.append((ident.name, [_label(db, u) for u in local]))
    return out


def last_sync(db: Database) -> dict[str, Any] | None:
    row = db.conn.execute(
        "SELECT * FROM sync_runs WHERE status IN ('ok','partial') ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return {
        "run_id": row["run_id"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "errors": json.loads(row["errors_json"] or "[]"),
    }


# ---------------------------------------------------------------------------
# Start over: remove the app's playlists and your playlist definitions
# ---------------------------------------------------------------------------

RETIRED_KEY = "retired_playlist_ids"
# User data wiped by a start-over (the backup keeps it). Duplicate decisions, the tag cache,
# your tag classes, the imported library and the run logs are kept.
WIPED_TABLES = (
    "feedback_log",
    "feedback",
    "identity_examples",
    "draft_members",
    "draft_runs",
    "playlist_snapshots",
    "managed_playlists",
    "identities",
    "proposals",
    "playlist_intents",
)


def retired_ids(db: Database) -> set[str]:
    return set(json.loads(db.get_meta(RETIRED_KEY) or "[]"))


def backup(db: Database, folder: Any, label: str) -> Any:
    """Copy the whole database (SQLite online backup) before something irreversible."""
    import sqlite3
    from pathlib import Path

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().replace(":", "").replace("-", "")[:15]
    dest = folder / f"curator-{label}-{stamp}.sqlite3"
    out = sqlite3.connect(dest)
    try:
        db.conn.backup(out)
    finally:
        out.close()
    return dest


def start_over(
    db: Database,
    client: SpotifyClient | None,
    *,
    backup_dir: Any,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Back up, remove every app-made playlist from your Spotify library, wipe definitions.

    Only playlists in the app's registry are removed (never your own playlists or Liked
    Songs). Their ids are remembered so a later import never mistakes them for yours, even if
    removing one failed."""
    say = progress or (lambda _m: None)
    path = backup(db, backup_dir, "before-start-over")
    say(f"Backup saved: {path}")
    reg = managed(db)
    ids = [e["spotify_id"] for e in reg.values() if e["status"] != "released"]
    removed, failed = [], []
    if ids and client is not None:
        for sid in ids:
            _guard(db, sid)
        say(f"Removing {len(ids)} app-made playlist(s) from your Spotify library ...")
        for sid in ids:
            try:
                client.remove_playlists_from_library([sid])
                removed.append(sid)
            except SpotifyAPIError as exc:
                failed.append({"spotify_id": sid, "error": exc.message})
    db.set_meta(
        RETIRED_KEY, json.dumps(sorted(retired_ids(db) | {e["spotify_id"] for e in reg.values()}))
    )
    counts = {}
    with db.transaction():
        for t in WIPED_TABLES:
            counts[t] = db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            db.conn.execute(f"DELETE FROM {t}")
    return {"backup": str(path), "removed": removed, "failed": failed, "wiped": counts}


def prune_retired(
    db: Database, client: SpotifyClient, *, progress: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Remove from Spotify the app-made playlists whose definition you retired or rejected
    (for example a copy retired by `identities dedupe`). Only app-made playlists, guarded."""
    say = progress or (lambda _m: None)
    rows = db.conn.execute(
        "SELECT m.identity_id, m.spotify_id, m.name FROM managed_playlists m "
        "JOIN identities i ON i.identity_id = m.identity_id "
        "WHERE i.status != 'approved' AND m.status != 'released'"
    ).fetchall()
    removed, failed = [], []
    for r in rows:
        _guard(db, r["spotify_id"])
        try:
            client.remove_playlists_from_library([r["spotify_id"]])
            removed.append(r["name"])
            say(f"Removed '{r['name']}' (retired copy) from Spotify")
        except SpotifyAPIError as exc:
            failed.append({"playlist": r["name"], "error": exc.message})
            continue
        db.conn.execute(
            "UPDATE managed_playlists SET status='released' WHERE identity_id=?",
            (r["identity_id"],),
        )
    if rows:
        db.set_meta(
            RETIRED_KEY,
            json.dumps(sorted(retired_ids(db) | {r["spotify_id"] for r in rows})),
        )
    return {"removed": removed, "failed": failed}
