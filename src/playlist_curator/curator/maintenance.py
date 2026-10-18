"""Phase 7: keep your playlists up to date as your library changes.

One maintenance run is the same steps you'd run by hand, in order:

1. ``import``: read Liked Songs and your playlists (unchanged playlists aren't re-downloaded).
2. ``enrich``: fetch tags, but only for songs that don't have them yet (the cache covers the rest).
3. ``draft``: rescore every playlist. Songs already in a playlist need less to stay than new
   songs need to get in (hysteresis, from Phase 5), so playlists don't flip-flop.
4. ``sync`` plan: work out what would change in Spotify.

Then the drift guards decide, playlist by playlist, whether a change is small and safe enough
to make without you. A playlist is **held** for your approval (``curator sync``) when:

- it doesn't exist in Spotify yet (new playlists are always approved by hand);
- the change is bigger than the limit (default: more than 10 songs or 25% of the playlist);
- the import was incomplete, or your library shrank suspiciously (default: by more than 10%),
  because missing data would look like songs to remove.

Nothing is written unless you turned on automatic updates (``curator maintain settings
--auto-apply on``); otherwise a run only prepares the preview. Every write goes through
``spotify.sync.apply``, so the Phase 6 guarantees still hold: only playlists the app created,
snapshots before and after, verification, rollback.

Note on "score only affected songs": rescoring the whole library takes a second or two at this
size, so every run rescores everything. That's simpler and can't miss a song; stability comes
from hysteresis and the guards, not from skipping work.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from playlist_curator.crate.importer import import_library
from playlist_curator.curator.scoring import build_drafts
from playlist_curator.metadata.descriptors import Descriptors, coverage, enrich, load_descriptors
from playlist_curator.spotify import sync
from playlist_curator.spotify.client import SpotifyClient
from playlist_curator.storage.database import Database, utcnow

log = logging.getLogger(__name__)

SETTINGS_KEY = "maintenance_settings"


@dataclass
class MaintenanceSettings:
    auto_apply: bool = False  # write allowed changes without asking
    max_changes: int = 10  # a playlist may always change by this many songs ...
    max_change_share: float = 0.25  # ... or this share of its size, whichever is larger
    max_library_shrink: float = 0.10  # hold everything if the library shrinks more than this
    enrich: bool = True  # fetch tags for new songs during the run

    def limit_for(self, size: int) -> int:
        return max(self.max_changes, math.ceil(self.max_change_share * size))


def load_settings(db: Database) -> MaintenanceSettings:
    raw = db.get_meta(SETTINGS_KEY)
    base = MaintenanceSettings()
    if not raw:
        return base
    data = json.loads(raw)
    known = {k: v for k, v in data.items() if k in asdict(base)}
    return MaintenanceSettings(**{**asdict(base), **known})


def save_settings(db: Database, **changes: Any) -> MaintenanceSettings:
    cur = asdict(load_settings(db))
    for k, v in changes.items():
        if v is None:
            continue
        if k not in cur:
            raise KeyError(k)
        cur[k] = v
    new = MaintenanceSettings(**cur)
    if new.max_changes < 0 or not 0 <= new.max_change_share <= 1:
        raise ValueError("limits must be ≥ 0 and the share between 0 and 1")
    if not 0 <= new.max_library_shrink <= 1:
        raise ValueError("library shrink limit must be between 0 and 1")
    db.set_meta(SETTINGS_KEY, json.dumps(asdict(new)))
    return new


@dataclass
class MaintenanceResult:
    run_id: int
    trigger: str
    mode: str  # preview | auto
    status: str  # ok | held | partial | failed | stopped
    import_status: str | None = None
    new_songs: int = 0
    removed_songs: int = 0
    recordings_before: int = 0
    recordings_after: int = 0
    tagged_new: int = 0
    draft_run: int | None = None
    ready: list[dict[str, Any]] = field(default_factory=list)  # small changes (applied if auto)
    held: list[dict[str, Any]] = field(default_factory=list)  # need your approval
    unchanged: int = 0
    applied: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    local_files: int = 0


def _count_recordings(db: Database) -> int:
    return int(db.conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0])


def _recording_ids(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT recording_id FROM recordings")}


def _change(pp: sync.PlaylistPlan) -> dict[str, Any]:
    return {
        "playlist": pp.name,
        "action": pp.action,
        "add": len(pp.add),
        "remove": len(pp.remove),
        "now": pp.current_count,
        "manual_edits": len(pp.keep_manual_adds) + len(pp.respect_manual_removes),
    }


def hold_reason(
    pp: sync.PlaylistPlan, settings: MaintenanceSettings, *, library_ok: bool
) -> str | None:
    """Why this playlist's change needs your approval, or None if it's safe to make."""
    if pp.action == "create":
        return "new playlist: create it yourself with `curator sync --apply`"
    if not library_ok:
        return "library data looks incomplete this run"
    n = len(pp.add) + len(pp.remove)
    limit = settings.limit_for(pp.current_count)
    if n > limit:
        return f"{n} songs would change (limit {limit})"
    return None


def run(
    db: Database,
    client: SpotifyClient,
    *,
    providers: list[Any] | None = None,
    settings: MaintenanceSettings | None = None,
    trigger: str = "manual",
    apply: bool | None = None,
    progress: Callable[[str], None] | None = None,
    descriptors: dict[str, Descriptors] | None = None,  # tests only; default reads the cache
) -> MaintenanceResult:
    """One maintenance pass. ``apply`` overrides the saved auto-apply setting (None = use it)."""
    say = progress or (lambda _m: None)
    cfg = settings or load_settings(db)
    do_apply = cfg.auto_apply if apply is None else apply
    mode = "auto" if do_apply else "preview"
    # A run that was cut off (closed window, PC shut down) never got to record its end.
    db.conn.execute(
        "UPDATE maintenance_runs SET status='stopped', finished_at=? WHERE status='running'",
        (utcnow(),),
    )
    cur = db.conn.execute(
        "INSERT INTO maintenance_runs (started_at, trigger, mode, status) VALUES (?,?,?,'running')",
        (utcnow(), trigger, mode),
    )
    res = MaintenanceResult(int(cur.lastrowid or 0), trigger, mode, "failed")
    try:
        _run(db, client, providers or [], cfg, do_apply, res, say, descriptors)
    except BaseException as exc:  # recorded (even Ctrl+C), then re-raised so callers see it
        res.status = "stopped" if isinstance(exc, KeyboardInterrupt) else "failed"
        res.errors.append(
            "stopped by you"
            if isinstance(exc, KeyboardInterrupt)
            else f"{type(exc).__name__}: {exc}"
        )
        _finish(db, res)
        raise
    _finish(db, res)
    return res


def _run(
    db: Database,
    client: SpotifyClient,
    providers: list[Any],
    cfg: MaintenanceSettings,
    do_apply: bool,
    res: MaintenanceResult,
    say: Callable[[str], None],
    descriptors: dict[str, Descriptors] | None,
) -> None:
    res.recordings_before = _count_recordings(db)
    known = _recording_ids(db)

    say("1/4 Importing your library ...")
    imp = import_library(client, db, progress=lambda _m: None)
    res.import_status = imp.status
    res.new_songs, res.removed_songs = imp.counts.new_tracks, imp.counts.removed_tracks
    res.recordings_after = _count_recordings(db)
    say(
        f"    {res.recordings_after} songs (+{res.new_songs} new, -{res.removed_songs} gone)"
        + (f"; {len(imp.errors)} playlist(s) couldn't be read" if imp.errors else "")
    )
    for e in imp.errors:
        res.errors.append(f"import: {e.get('name', '')} {e.get('error', '')}".strip())

    library_ok = imp.status == "ok"
    if res.recordings_before:
        shrink = 1 - res.recordings_after / res.recordings_before
        if shrink > cfg.max_library_shrink:
            library_ok = False
            res.errors.append(
                f"library shrank by {shrink:.0%} (limit {cfg.max_library_shrink:.0%}); "
                "nothing applied automatically"
            )

    # New songs, plus any song with no tags at all (e.g. imported by a run you stopped).
    # Songs looked up before and not found are cache hits, so this stays quick.
    untagged = {rid for rid, d in load_descriptors(db).items() if not d.by_source}
    new_ids = (_recording_ids(db) - known) | untagged
    if cfg.enrich and providers and new_ids:
        # Only the songs that are new this run. Backfilling old songs (e.g. a MusicBrainz
        # pass you stopped early) is `curator enrich`'s job; at ~1 request/second it can take
        # an hour, which a scheduled run shouldn't.
        say(f"2/4 Tagging {len(new_ids)} new or untagged song(s) ...")
        before = _tagged(db)
        stats = enrich(db, providers, only=new_ids, retry_invalid=False, progress=lambda _m: None)
        res.tagged_new = max(0, _tagged(db) - before)
        for st in stats:
            if st.stopped_early:
                res.errors.append(f"{st.provider}: stopped early: {st.stopped_early}")
        say(f"    {res.tagged_new} newly tagged")
    else:
        say("2/4 Tagging: " + ("no new songs" if not new_ids else "skipped"))

    say("3/4 Rebuilding drafts ...")
    draft = build_drafts(db, descriptors=descriptors)
    res.draft_run = draft.run_id

    say("4/4 Comparing with Spotify ...")
    plan = sync.plan(db, client)
    allowed: list[sync.PlaylistPlan] = []
    for pp in plan.playlists:
        if pp.local_files:
            res.local_files += len(pp.local_files)
        if pp.action in ("unchanged", "skip"):
            res.unchanged += 1
            continue
        reason = hold_reason(pp, cfg, library_ok=library_ok)
        if reason:
            res.held.append({**_change(pp), "reason": reason})
        else:
            res.ready.append(_change(pp))
            allowed.append(pp)

    if do_apply and allowed:
        say(f"Writing {len(allowed)} playlist(s) to Spotify ...")
        out = sync.apply(db, client, sync.SyncPlan(plan.draft_run, allowed), progress=say)
        res.applied = out.applied
        res.errors += [f"{e['playlist']}: {e['error']}" for e in out.errors]
        if out.errors:
            res.status = "partial"
    if res.status != "partial":
        res.status = "held" if res.held else "ok"


def _tagged(db: Database) -> int:
    return coverage(db)["with_any_tags"]


def _finish(db: Database, res: MaintenanceResult) -> None:
    summary = {k: v for k, v in asdict(res).items() if k not in ("run_id", "errors")}
    db.conn.execute(
        "UPDATE maintenance_runs SET finished_at=?, status=?, summary_json=?, errors_json=? "
        "WHERE run_id=?",
        (utcnow(), res.status, json.dumps(summary), json.dumps(res.errors), res.run_id),
    )


def last_runs(db: Database, n: int = 10) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT * FROM maintenance_runs ORDER BY run_id DESC LIMIT ?", (n,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["summary"] = json.loads(r["summary_json"] or "{}")
        d["errors"] = json.loads(r["errors_json"] or "[]")
        out.append(d)
    return out


def report(res: MaintenanceResult) -> str:
    """Plain-text summary, also saved to data/reports/ after each run."""
    lines = [
        f"Maintenance run #{res.run_id} ({res.trigger}, "
        + ("automatic updates on" if res.mode == "auto" else "preview only")
        + f"): {res.status}",
        f"Library: {res.recordings_after} songs (+{res.new_songs} new, -{res.removed_songs} gone)"
        + (f", {res.tagged_new} newly tagged" if res.tagged_new else ""),
    ]
    if res.applied:
        lines.append("Updated in Spotify:")
        lines += [f"  {a['playlist']}: +{a['added']} / -{a['removed']}" for a in res.applied]
    elif res.ready:
        lines.append(
            "Ready (small changes)"
            + (": run `curator sync --apply`" if res.mode == "preview" else "")
            + ":"
        )
        lines += [f"  {c['playlist']}: +{c['add']} / -{c['remove']}" for c in res.ready]
    if res.held:
        lines.append("Waiting for your approval (`curator sync` to preview, then --apply):")
        lines += [
            f"  {c['playlist']}: +{c['add']} / -{c['remove']}  ({c['reason']})" for c in res.held
        ]
    if not (res.applied or res.ready or res.held):
        lines.append("Nothing to change in Spotify.")
    if res.local_files:
        lines.append(f"{res.local_files} local file(s) to add by hand: curator sync local-files")
    for e in res.errors:
        lines.append(f"! {e}")
    return "\n".join(lines)


def save_report(data_dir: Path, res: MaintenanceResult) -> Path:
    folder = data_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "latest-maintenance.txt"
    path.write_text(report(res) + "\n", encoding="utf-8")
    return path
