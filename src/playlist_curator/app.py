"""Streamlit interface.

Launch with ``curator ui`` (or ``streamlit run src/playlist_curator/app.py``).

Current views: Library (crate overview and songs), Duplicates (review suggested matches,
inspect and split merged recordings), Local files, Source playlists, Import history.
Later phases add Playlist Collection, Playlist Details and Spotify Sync.
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from playlist_curator.config import ConfigError, load_settings
from playlist_curator.crate import deduplication as dedup
from playlist_curator.crate.importer import import_library
from playlist_curator.crate.inventory import (
    decisions_table,
    duplicate_groups,
    local_files_table,
    pending_suggestions,
    playlist_table,
    summarize,
    track_table,
)
from playlist_curator.curator.analysis import analyze
from playlist_curator.spotify.auth import AuthError, TokenManager, TokenStore
from playlist_curator.spotify.client import SpotifyAPIError, SpotifyClient
from playlist_curator.storage.database import Database

st.set_page_config(page_title="Playlist Curator", layout="wide")
settings = load_settings()


def run_import(force: bool) -> None:
    try:
        settings.validate()
        client = SpotifyClient(TokenManager(settings))
        with (
            st.status("Importing from Spotify ...", expanded=True) as status,
            Database(settings.db_path) as db,
        ):
            result = import_library(client, db, force=force, progress=st.write)
            status.update(label=f"Import {result.status}", state="complete")
    except (ConfigError, AuthError, SpotifyAPIError) as exc:
        st.error(str(exc))


def act(fn, *args) -> None:
    """Run a write against a fresh connection, then refresh the page."""
    with Database(settings.db_path) as db:
        fn(db, *args)
    st.rerun()


def describe(t: dict) -> str:
    if t.get("local_file"):
        where = "local file"
    else:
        kind = f" · {t['album_type']}" if t.get("album_type") else ""
        where = f"{t.get('album') or '?'}{kind} · {t.get('release_date') or '?'}"
    length = ""
    if t.get("duration_s"):
        m, s = divmod(t["duration_s"], 60)
        length = f" · {m}:{s:02d}"
    isrc = f" · ISRC {t['isrc']}" if t.get("isrc") else ""
    return (
        f"**{t.get('name')}** — {t.get('artists') or '?'}  \n"
        f"{where}{length}{isrc} · in {t.get('times_in_sources', 0)} source(s)"
    )


with st.sidebar:
    st.header("Playlist Curator")
    logged_in = TokenStore(settings.token_path).load() is not None
    if logged_in:
        st.success("Spotify tokens found")
    else:
        st.warning("Not logged in. Run `curator auth login` in a terminal.")
    force = st.checkbox("Re-read unchanged playlists", value=False)
    if st.button("Import from Spotify", disabled=not logged_in, type="primary"):
        run_import(force)
    st.caption("Import only reads from Spotify. Nothing in your account is changed.")

if not settings.db_path.exists():
    st.info("No library imported yet. Log in, then click **Import from Spotify**.")
    st.stop()

with Database(settings.db_path) as db:
    dedup.ensure_current(db)
    s = summarize(db)
    tracks = pd.DataFrame(track_table(db))
    playlists = pd.DataFrame(playlist_table(db))
    suggestions = pending_suggestions(db)
    groups = duplicate_groups(db)
    decisions = decisions_table(db)
    local_files = pd.DataFrame(local_files_table(db))
    runs = db.last_runs(20)
    report = analyze(db)

st.title("Library")

c = st.columns(5)
c[0].metric("Unique songs", s.unique_recordings, help="Copies of the same recording count once")
c[1].metric("In Liked Songs", s.rec_in_liked)
c[2].metric("In playlists", s.rec_in_playlists)
c[3].metric("In both", s.rec_in_both)
c[4].metric("Local files", s.local_tracks, help=f"{s.local_matched} matched to a catalog song")

c = st.columns(5)
c[0].metric("Only in playlists", s.rec_playlists_only, help="Songs you kept but never liked")
c[1].metric("Only liked", s.rec_liked_only)
c[2].metric(
    "Duplicate copies merged",
    s.duplicate_uris_merged,
    help=f"{s.merged_groups} recordings had more than one Spotify copy",
)
c[3].metric("Matches to review", s.pending_title_suggestions + s.pending_local_suggestions)
c[4].metric(
    "Playlists unreadable",
    s.playlists_contents_unavailable,
    help="Spotify only returns contents of playlists you own or collaborate on.",
)
st.caption("Unassigned songs will appear here once playlist assignment exists (Phase 4).")

tab_analysis, tab_songs, tab_dups, tab_local, tab_playlists, tab_runs = st.tabs(
    [
        "Playlist analysis",
        "Songs",
        f"Duplicates ({len(suggestions)} to review)",
        "Local files",
        "Source playlists",
        "Import history",
    ]
)

with tab_analysis:
    st.caption(
        f"{report.crate_tagged} of {report.crate_recordings} songs have tags. "
        "Run `curator enrich` to fetch more. Cohesion and similarity run 0–1 and are "
        "heuristics, not probabilities."
    )
    if report.crate_tagged:
        st.caption(
            "Tag data from [MusicBrainz](https://musicbrainz.org) and "
            "[Last.fm](https://www.last.fm), used for personal, non-commercial purposes."
        )
    rows = [
        {
            "playlist": p.name,
            "songs": p.songs,
            "liked": f"{round(100 * p.liked_share)}%",
            "era (median)": p.year_median,
            "tagged": p.tagged,
            "cohesion": p.cohesion,
            "early vs late": p.drift_similarity,
            "last added": p.last_added,
            "flags": len(p.flags),
        }
        for p in report.playlists
    ]
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    for p in report.playlists:
        with st.expander(f"{p.name}  ·  {p.songs} songs  ·  {len(p.flags)} flag(s)"):
            if p.top_tags:
                st.markdown(
                    "**Sound:** "
                    + ", ".join(
                        f"{t['tag']} ({round(100 * t['support'])}%, ×{t['lift']})"
                        for t in p.top_tags
                    )
                )
            if p.year_median:
                st.markdown(
                    f"**Era:** median {p.year_median} ({p.year_p10}–{p.year_p90}); "
                    + ", ".join(f"{d} {round(100 * x)}%" for d, x in p.decades)
                )
            if p.top_artists:
                st.markdown(
                    f"**Artists:** {p.unique_artists} unique; top "
                    + ", ".join(f"{a} {round(100 * x)}%" for a, x in p.top_artists)
                )
            for f in p.flags:
                st.warning(f)
            if p.outliers:
                st.markdown("**Least like the rest of the playlist**")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {"song": o.title, "artists": o.artists, "similarity": o.similarity}
                            for o in p.outliers
                        ]
                    ),
                    hide_index=True,
                )
    if report.pairs:
        st.subheader("Overlapping playlists")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "a": x.a,
                        "b": x.b,
                        "shared songs": x.shared_songs,
                        "share of smaller": x.containment,
                        "sound similarity": x.profile_similarity,
                        "why": x.flag,
                    }
                    for x in report.pairs
                ]
            ),
            width="stretch",
            hide_index=True,
        )

with tab_songs:
    if tracks.empty:
        st.write("No songs imported.")
    else:
        f1, f2, f3 = st.columns([2, 1, 1])
        query = f1.text_input("Search title, artist or album")
        source = f2.selectbox(
            "Show",
            ["All", "Liked", "Not liked", "Local files", "In 3+ playlists", "Has duplicates"],
        )
        names = sorted(n for n in playlists["name"]) if not playlists.empty else []
        in_playlist = f3.selectbox("In playlist", ["Any", *names])
        view = tracks
        if query:
            q = query.lower()
            view = view[
                view["name"].str.lower().str.contains(q, regex=False)
                | view["artists"].str.lower().str.contains(q, regex=False)
                | view["album"].fillna("").str.lower().str.contains(q, regex=False)
            ]
        if source == "Liked":
            view = view[view["liked"]]
        elif source == "Not liked":
            view = view[~view["liked"]]
        elif source == "Local files":
            view = view[view["local_file"]]
        elif source == "In 3+ playlists":
            view = view[view["n_playlists"] >= 3]
        elif source == "Has duplicates":
            view = view[view["copies_of_recording"] > 1]
        if in_playlist != "Any":
            view = view[view["playlists"].apply(lambda names: in_playlist in names)]
        st.caption(f"{len(view)} of {len(tracks)} Spotify copies / local entries")
        st.dataframe(view, width="stretch", hide_index=True)

with tab_dups:
    st.subheader("Waiting for your decision")
    st.caption(
        "Same ISRC is merged automatically. These have different ISRCs (or are local files), "
        "so you decide. Different versions (live, remix, edit, ...) are never suggested."
    )
    if not suggestions:
        st.success("Nothing to review.")
    for item in suggestions:
        with st.container(border=True):
            label = "Local file match" if item["kind"] == "local_match" else "Same song?"
            st.markdown(f"**{label}** · {item['strength']} · `{item['id']}`")
            st.caption(item["reason"])
            a, b = st.columns(2)
            a.markdown(describe(item["a"]))
            b.markdown(describe(item["b"]))
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("Same recording", key=f"same-{item['id']}", type="primary"):
                act(dedup.decide, item["a"]["uri"], item["b"]["uri"], "same")
            if b2.button("Different", key=f"diff-{item['id']}"):
                act(dedup.decide, item["a"]["uri"], item["b"]["uri"], "different")

    st.subheader(f"Merged recordings ({len(groups)})")
    st.caption("★ marks the copy used when writing playlists.")
    for g in groups:
        with st.expander(f"{g['title']} — {g['artists']}  ({len(g['members'])} copies)"):
            for m in g["members"]:
                left, right = st.columns([5, 1])
                star = "★ " if m["primary"] else ""
                left.markdown(f"{star}{describe(m)}  \nGrouped by: {m['evidence']}")
                if right.button("Split out", key=f"split-{m['uri']}"):
                    act(dedup.split_out, m["uri"])

    if decisions:
        st.subheader("Your decisions")
        for d in decisions:
            left, right = st.columns([5, 1])
            left.markdown(
                f"`{d['id']}` **{d['decision']}** · {d['a'].get('name')} ↔ {d['b'].get('name')}"
            )
            if right.button("Undo", key=f"undo-{d['id']}"):
                act(dedup.undo_decision, d["pair_key"])

with tab_local:
    st.caption(
        "Local files can't be added to playlists through Spotify's API. Matching them to "
        "catalog songs lets later phases tell you where each one belongs."
    )
    if local_files.empty:
        st.write("No local files found in your playlists.")
    else:
        st.dataframe(local_files, width="stretch", hide_index=True)

with tab_playlists:
    st.dataframe(playlists, width="stretch", hide_index=True)

with tab_runs:
    for r in runs:
        counts = json.loads(r["counts_json"]) if r["counts_json"] else {}
        errors = json.loads(r["errors_json"]) if r["errors_json"] else []
        with st.expander(f"#{r['id']}  {r['started_at']}  —  {r['status']}"):
            st.json({"counts": counts, "errors": errors})
