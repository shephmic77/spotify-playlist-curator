"""Streamlit interface.

Launch with ``curator ui`` (or ``streamlit run src/playlist_curator/app.py``).

Phase 1 provides the Library view: what is in the crate, where each song came from, and
which playlists could not be read. Later phases add Playlist Collection, Playlist Details
and Spotify Sync views on top of the same database.
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from playlist_curator.config import ConfigError, load_settings
from playlist_curator.crate.importer import import_library
from playlist_curator.crate.inventory import playlist_table, summarize, track_table
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
    s = summarize(db)
    tracks = pd.DataFrame(track_table(db))
    playlists = pd.DataFrame(playlist_table(db))
    runs = db.last_runs(20)

st.title("Library")

c = st.columns(5)
c[0].metric("Unique songs", s.unique_tracks)
c[1].metric("In Liked Songs", s.in_liked)
c[2].metric("In playlists", s.in_playlists)
c[3].metric("In both", s.in_both)
c[4].metric("Local files", s.local_tracks)

c = st.columns(5)
c[0].metric("Only in playlists", s.playlists_only, help="Songs you kept but never liked")
c[1].metric("Only liked", s.liked_only)
c[2].metric("Repeated in a playlist", s.repeated_within_playlist)
c[3].metric(
    "Possible duplicates",
    s.isrc_groups_with_multiple_uris,
    help="Groups of different Spotify URIs sharing an ISRC. Not merged yet (Phase 2).",
)
c[4].metric(
    "Playlists unreadable",
    s.playlists_contents_unavailable,
    help="Spotify only returns contents of playlists you own or collaborate on.",
)
st.caption("Unassigned songs will appear here once playlist assignment exists (Phase 4).")

tab_tracks, tab_playlists, tab_runs = st.tabs(["Songs", "Source playlists", "Import history"])

with tab_tracks:
    if tracks.empty:
        st.write("No songs imported.")
    else:
        f1, f2, f3 = st.columns([2, 1, 1])
        query = f1.text_input("Search title, artist or album")
        source = f2.selectbox(
            "Source", ["All", "Liked", "Not liked", "Local files", "In 3+ playlists"]
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
        if in_playlist != "Any":
            view = view[view["playlists"].apply(lambda names: in_playlist in names)]
        st.caption(f"{len(view)} of {len(tracks)} songs")
        st.dataframe(view, use_container_width=True, hide_index=True)

with tab_playlists:
    st.dataframe(playlists, use_container_width=True, hide_index=True)

with tab_runs:
    for r in runs:
        counts = json.loads(r["counts_json"]) if r["counts_json"] else {}
        errors = json.loads(r["errors_json"]) if r["errors_json"] else []
        with st.expander(f"#{r['id']}  {r['started_at']}  —  {r['status']}"):
            st.json({"counts": counts, "errors": errors})
