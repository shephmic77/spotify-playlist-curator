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
from playlist_curator.curator import describe as ds
from playlist_curator.curator import discovery as disc
from playlist_curator.curator import feedback as fb
from playlist_curator.curator import identities as idn
from playlist_curator.curator import intents as it
from playlist_curator.curator import scoring as sc
from playlist_curator.curator.analysis import analyze
from playlist_curator.spotify import sync
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
    intents = {i.playlist_id: i for i in it.list_intents(db)}
    proposals = disc.load_proposals(db)
    identities = idn.list_identities(db)
    readable = [r for r in db.list_playlists() if r["contents_status"] == "fetched"]
    draft_run = sc.latest_run(db)
    drafts = {i.identity_id: sc.load_draft(db, i.identity_id, draft_run) for i in identities}

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
if draft_run is not None:
    in_some = {
        r["recording_id"] for rows_ in drafts.values() for r in rows_ if r["decision"] == "include"
    }
    st.caption(
        f"In at least one draft playlist: {len(in_some)} songs · in none: "
        f"{s.unique_recordings - len(in_some)} (allowed: songs only go where they fit)."
    )
else:
    st.caption("Build drafts (Draft playlists tab) to see which songs fit no playlist.")

(
    tab_new,
    tab_drafts,
    tab_sync,
    tab_analysis,
    tab_songs,
    tab_dups,
    tab_local,
    tab_playlists,
    tab_runs,
) = st.tabs(
    [
        f"New collection ({len(proposals)} to review)",
        "Draft playlists",
        "Spotify sync",
        "Old playlist analysis",
        "Songs",
        f"Duplicates ({len(suggestions)} to review)",
        "Local files",
        "Source playlists",
        "Import history",
    ]
)


def regenerate() -> None:
    with Database(settings.db_path) as db:
        props, _ = disc.propose(db)
        disc.save_proposals(db, props)
    st.rerun()


with tab_new:
    st.caption(
        "Your new collection is built from scratch. Old playlists are only evidence: describe "
        "what each was for, generate proposals, then accept the concepts you want."
    )
    left, right = st.columns([3, 2])

    with right:
        st.subheader("Describe a playlist in your words")
        st.caption(
            "Built-in dictionary for common moods and situations; with ANTHROPIC_API_KEY set, "
            "Claude translates anything. Only your description is sent, never your library. "
            "Examples are matched in your library only."
        )
        with st.form("describe"):
            text = st.text_area(
                "Description",
                placeholder="music to study to, chill, lyrics ok but not distracting, not loud",
                height=80,
            )
            examples = st.text_input("Examples (artists or songs, comma-separated)", "")
            new_name = st.text_input("Playlist name (to create)", "")
            c1, c2 = st.columns(2)
            preview = c1.form_submit_button("Preview")
            create = c2.form_submit_button("Create playlist", type="primary")
        if (preview or create) and text.strip():
            try:
                recipe = ds.translate(
                    text,
                    engine="auto",
                    api_key=settings.anthropic_api_key,
                    model=settings.anthropic_model,
                )
            except ds.DescribeError as exc:
                st.error(str(exc))
                recipe = None
            if recipe is not None:
                with Database(settings.db_path) as db:
                    desc_all = sc.load_descriptors(db)
                    refs = [x.strip() for x in examples.split(",") if x.strip()]
                    matches = ds.resolve_examples(db, refs) if refs else []
                    uris = [u for m in matches for u in m.uris]
                    for tag, support in ds.example_tags(uris, db, desc_all):
                        if tag not in recipe.tags and tag not in recipe.avoid:
                            recipe.tags[tag] = round(0.8 * support, 2)
                    report = ds.ground(recipe, desc_all)
                    if create and new_name.strip():
                        ds.apply_recipe(db, recipe, create=new_name.strip(), example_uris=uris)
                st.caption(f"engine: {recipe.engine}")
                for m in matches:
                    if m.kind == "none":
                        st.warning(f"'{m.query}' isn't in your library")
                    else:
                        st.write(f"✓ {m.query}: {len(m.uris)} song(s) ({m.kind})")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "tag": r.tag,
                                "weight": r.weight,
                                "your songs": r.songs,
                                "closest in library": ", ".join(t for t, _ in r.suggestions),
                            }
                            for r in report
                        ]
                    ),
                    hide_index=True,
                )
                if recipe.avoid:
                    st.write("Avoid: " + ", ".join(recipe.avoid))
                if create and new_name.strip():
                    st.success(f"Created '{new_name.strip()}'. Build drafts to fill it.")
                elif create:
                    st.warning("Give the playlist a name to create it.")

        st.subheader(f"Your new playlists ({len(identities)})")
        if not identities:
            st.write("None yet. Accept a proposal to create one.")
        for ident in identities:
            with st.expander(f"#{ident.identity_id} {ident.name} · {ident.kind}"):
                with st.form(f"ident-{ident.identity_id}"):
                    name = st.text_input("Name", ident.name)
                    concept = st.text_area("Concept", ident.concept, height=70)
                    tags = st.text_input(
                        "Tags (tag=weight, comma-separated)",
                        ", ".join(f"{t}={w:g}" for t, w in (ident.rules.get("tags") or {}).items()),
                    )
                    avoid = st.text_input(
                        "Avoid tags", ", ".join(ident.rules.get("avoid_tags") or [])
                    )
                    require = st.text_input(
                        "Must have one of these tags",
                        ", ".join(ident.rules.get("require_tags") or []),
                    )
                    yrs = ident.rules.get("years")
                    years = st.text_input(
                        "Preferred years (FROM-TO or none)", f"{yrs[0]}-{yrs[1]}" if yrs else "none"
                    )
                    auto = st.checkbox("Let the app add matching songs", ident.auto_maintain)
                    c1, c2 = st.columns(2)
                    save = c1.form_submit_button("Save", type="primary")
                    retire = c2.form_submit_button("Retire")
                if save:
                    new_tags = idn.parse_tag_weights(
                        [x.strip() for x in tags.split(",") if x.strip()]
                    )
                    with Database(settings.db_path) as db:
                        current = set(ident.rules.get("tags") or {})
                        idn.edit_identity(
                            db,
                            ident.identity_id,
                            name=name,
                            concept=concept,
                            add_tags=new_tags,
                            remove_tags=list(current - set(new_tags)),
                            avoid_tags=[x.strip() for x in avoid.split(",") if x.strip()],
                            unavoid_tags=list(ident.rules.get("avoid_tags") or []),
                            require_tags=[x.strip() for x in require.split(",") if x.strip()],
                            unrequire_tags=list(ident.rules.get("require_tags") or []),
                            years=idn.parse_years(years),
                            auto_maintain=auto,
                        )
                    st.rerun()
                if retire:
                    act(idn.set_status, ident.identity_id, "retired")

    with left:
        st.subheader("Proposals")
        if st.button("Generate / refresh proposals", type="primary"):
            regenerate()
        if not proposals:
            st.write("No pending proposals. Describe your old playlists below, then generate.")
        labels = {
            "from_description": "from your descriptions",
            "from_library": "found in your library",
            "handpicked": "hand-picked (optional)",
        }
        for prop in proposals:
            ev = prop.evidence
            with st.container(border=True):
                st.markdown(f"**{prop.title}** · {labels[prop.action]} · `#{prop.proposal_id}`")
                st.caption(prop.draft.concept)
                if ev.get("from_playlists"):
                    st.markdown(
                        "From old playlists: "
                        + ", ".join(f"{x['name']} ({x['songs']})" for x in ev["from_playlists"])
                        + f" · {ev.get('example_songs', 0)} example songs"
                    )
                if ev.get("cluster_songs"):
                    st.markdown(f"{ev['cluster_songs']} songs in your library cluster here")
                if ev.get("top_tags"):
                    st.markdown(
                        "Sound: "
                        + ", ".join(
                            f"{t['tag']} ({round(100 * t['support'])}%, ×{t['lift']})"
                            for t in ev["top_tags"][:6]
                        )
                    )
                if prop.draft.rules.get("years"):
                    lo, hi = prop.draft.rules["years"]
                    st.markdown(f"Era from your description: {lo}–{hi} (preferred)")
                for sim in ev.get("similar_to", []):
                    st.info(f"Sounds like '{sim['title']}' ({sim['sim']}): you may only want one.")
                c1, c2, c3 = st.columns([2, 1, 1])
                new_name = c1.text_input(
                    "Name",
                    prop.title,
                    key=f"pname-{prop.proposal_id}",
                    label_visibility="collapsed",
                )
                if c2.button("Accept", key=f"acc-{prop.proposal_id}", type="primary"):
                    act(lambda db, pid=prop.proposal_id, n=new_name: disc.accept(db, pid, name=n))
                if c3.button("Reject", key=f"rej-{prop.proposal_id}"):
                    act(disc.reject, prop.proposal_id)

        st.subheader("What your old playlists were for")
        st.caption(
            "sound / mood / situation / era can become concepts · handpicked comes back as "
            "optional · source / catch_all / mixed / unknown just add their songs to the pool"
        )
        for r in readable:
            cur = intents.get(r["spotify_id"])
            with (
                st.expander(
                    f"{r['name']} · {r['stored_entries']} songs · "
                    + (f"{cur.kind}: {cur.title}" if cur else "not described")
                ),
                st.form(f"intent-{r['spotify_id']}"),
            ):
                title = st.text_input("Short name", cur.title if cur else "")
                text = st.text_input("What it was for", cur.intent if cur else "")
                kind = st.selectbox(
                    "Kind",
                    it.KINDS,
                    index=it.KINDS.index(cur.kind) if cur else len(it.KINDS) - 1,
                )
                if st.form_submit_button("Save"):
                    act(it.set_intent, r["spotify_id"], text or title, kind, title)


with tab_drafts:
    st.caption(
        "Every song is scored against each playlist's own definition; a song can be in several "
        "playlists or none. Scores rank songs and aren't probabilities. Nothing is sent to "
        "Spotify from here."
    )
    if st.button("Build / refresh drafts", type="primary", disabled=not identities):
        with Database(settings.db_path) as db:
            sc.build_drafts(db)
        st.rerun()
    if draft_run is None:
        st.info("No drafts yet. Accept some playlists, then build drafts.")
    else:
        overview = [
            {
                "playlist": i.name,
                "songs": sum(1 for r in drafts[i.identity_id] if r["decision"] == "include"),
                "to review": sum(1 for r in drafts[i.identity_id] if r["decision"] == "review"),
                "hand-picked": not i.auto_maintain,
            }
            for i in identities
        ]
        st.dataframe(pd.DataFrame(overview), width="stretch", hide_index=True)
        names = {i.name: i.identity_id for i in identities}
        pick = st.selectbox("Playlist", list(names))
        rows = drafts.get(names[pick], []) if pick else []

        with Database(settings.db_path) as db:
            uri_of = {
                r[0]: r[1]
                for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings")
            }
            current = {d["uri"]: d["decision"] for d in fb.decisions_for(db, names[pick])}
        choices = ["", "keep", "reject", "essential", "uncertain"]

        def table(decision: str) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {
                        "your call": current.get(uri_of.get(r["recording_id"], ""), ""),
                        "score": r["score"],
                        "song": r["title"],
                        "artists": r["artists"],
                        "tag fit": r["tag_score"],
                        "like examples": r["example_score"],
                        "evidence": r["evidence"],
                        "why": "; ".join(r["reasons"]),
                        "uri": uri_of.get(r["recording_id"], ""),
                    }
                    for r in rows
                    if r["decision"] == decision
                ]
            )

        st.caption(
            "Set 'your call' and press Save: keep (an example for this playlist), reject (never "
            "here again; similar songs are scored down here too), essential (always in), "
            "uncertain (just noted). Decisions only affect this playlist."
        )
        editors = {}
        for decision, label in (("include", "In the draft"), ("review", "Borderline")):
            df = table(decision)
            st.markdown(f"**{label}** ({len(df)})")
            if df.empty:
                continue
            editors[decision] = (
                df,
                st.data_editor(
                    df,
                    key=f"edit-{pick}-{decision}",
                    width="stretch",
                    hide_index=True,
                    disabled=[c for c in df.columns if c != "your call"],
                    column_config={
                        "your call": st.column_config.SelectboxColumn(options=choices),
                        "uri": None,
                    },
                ),
            )
        if editors and st.button("Save my calls", type="primary"):
            changed = 0
            with Database(settings.db_path) as db:
                ident = idn.get_identity(db, names[pick])
                for before, after in editors.values():
                    for (_, b), (_, a) in zip(before.iterrows(), after.iterrows(), strict=True):
                        if a["your call"] and a["your call"] != b["your call"]:
                            fb.decide_uri(db, ident, a["uri"], a["your call"])
                            changed += 1
            st.success(f"Saved {changed} decision(s). Rebuild drafts to apply them.")

        with st.expander("Progress: acceptance, rejections, changes"):
            with Database(settings.db_path) as db:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "playlist": x.name,
                                "kept": x.kept,
                                "rejected": x.rejected,
                                "essential": x.essential,
                                "acceptance": x.acceptance_rate,
                                "manual adds": x.manual_adds,
                            }
                            for x in fb.stats(db)
                        ]
                    ),
                    hide_index=True,
                )
                sugg = fb.suggest_avoid_tags(db, names[pick], sc.load_descriptors(db))
            for x in sugg:
                st.info(
                    f"Your rejects in {pick} often have '{x['tag']}' "
                    f"({round(100 * x['in_rejected'])}% vs {round(100 * x['in_examples'])}% of "
                    "examples). Add it to 'Avoid tags' in New collection if that's the pattern."
                )


with tab_sync:
    st.caption(
        "Writes only to playlists this app creates (private). Your old playlists and Liked "
        "Songs are never touched. Every change is previewed, saved for rollback, and verified."
    )
    with Database(settings.db_path) as db:
        reg = sync.managed(db)
        last = sync.last_sync(db)
        local_actions = sync.local_file_actions(db)
    c1, c2 = st.columns(2)
    c1.metric("Playlists in Spotify (managed)", len(reg))
    c2.metric("Last successful sync", last["finished_at"][:16] if last else "never")
    if st.button("Preview changes", type="primary", disabled=draft_run is None):
        try:
            with Database(settings.db_path) as db:
                st.session_state["sync_plan"] = sync.plan(db, SpotifyClient(TokenManager(settings)))
        except (sync.SyncError, ConfigError, AuthError, SpotifyAPIError) as exc:
            st.error(str(exc))
    plan_obj = st.session_state.get("sync_plan")
    if plan_obj is not None:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "playlist": x.name,
                        "action": x.action,
                        "now": x.current_count,
                        "add": len(x.add),
                        "remove": len(x.remove),
                        "your Spotify edits kept": len(x.keep_manual_adds)
                        + len(x.respect_manual_removes),
                        "local files (by hand)": len(x.local_files),
                        "note": x.note,
                    }
                    for x in plan_obj.playlists
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        if plan_obj.writes:
            ok = st.checkbox(
                f"I've checked the preview. Write changes to {plan_obj.writes} playlist(s)."
            )
            if st.button("Apply to Spotify", disabled=not ok):
                try:
                    with Database(settings.db_path) as db:
                        res = sync.apply(
                            db,
                            SpotifyClient(TokenManager(settings)),
                            plan_obj,
                            progress=st.write,
                        )
                    st.success(f"Sync {res.status}: {len(res.applied)} playlist(s) written.")
                    for e in res.errors:
                        st.error(f"{e['playlist']}: {e['error']}")
                    st.session_state.pop("sync_plan", None)
                except (ConfigError, AuthError, SpotifyAPIError) as exc:
                    st.error(str(exc))
        else:
            st.success("Nothing to change in Spotify.")
    if last and last["errors"]:
        for e in last["errors"]:
            st.warning(f"Last sync: {e['playlist']}: {e['error']}")
    if local_actions:
        st.subheader("Add these local files by hand")
        st.caption(
            "Spotify's API can't add local files. In the desktop app: Your Library → Local "
            "Files → right-click → Add to playlist."
        )
        for name, songs in local_actions:
            st.markdown(f"**{name}**: " + "; ".join(songs))
    if reg:
        st.subheader("Managed playlists")
        for entry in reg.values():
            st.markdown(
                f"[{entry['name']}](https://open.spotify.com/playlist/{entry['spotify_id']}) · "
                f"last synced {entry['last_synced_at'] or '-'} · rollback: "
                f'`curator rollback "{entry["name"]}"`'
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
