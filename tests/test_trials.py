# ruff: noqa: F811  (setup is a pytest fixture imported from test_sync)
from playlist_curator.crate.importer import import_library
from playlist_curator.curator import identities as idn
from playlist_curator.curator import scoring as sc
from playlist_curator.curator import trials
from playlist_curator.spotify import sync
from test_sync import by_name, descriptors, setup  # noqa: F401

EL_PASO = "spotify:track:cat_elpaso"


def night(db):
    return idn.get_identity(db, "Night Drive")


def in_crate(db, uri):
    return db.conn.execute("SELECT 1 FROM recording_members WHERE uri=?", (uri,)).fetchone()


def test_add_finds_the_exact_song_and_asks_spotify_one_search_per_song(fake, client, db, setup):
    seen = []
    fake.on_request = lambda r: seen.append(r)
    res = trials.add(
        db,
        night(db),
        ["El Paso - Marty Robbins", "Midnight City - M83", "Nope - Nobody", "El Paso"],
        client.search_tracks,
    )
    assert [r.status for r in res] == ["added", "in_library", "not_found", "already_trial"]
    assert res[0].label == "El Paso — Marty Robbins"  # the studio version, not "El Paso - Live"
    searches = [r for r in seen if r.url.path == "/v1/search"]
    assert len(searches) == 4
    first = searches[0].url.params
    assert first["q"] == 'track:"El Paso" artist:"Marty Robbins"'
    assert first["type"] == "track" and int(first["limit"]) <= 10
    assert "market" not in first  # market=from_token gave 403 "Insufficient client scope"
    assert not in_crate(db, EL_PASO)  # pending songs are not in your library
    assert fake.write_bodies == []  # adding a trial never writes to Spotify by itself


def test_pending_trial_goes_into_the_playlist_only(fake, client, db, setup):
    trials.add(db, night(db), ["El Paso - Marty Robbins"], client.search_tracks)
    p = by_name(sync.plan(db, client))["Night Drive"]
    assert EL_PASO in p.add
    assert "(trial)" in sync._label(db, EL_PASO)
    res = sync.apply(db, client, sync.plan(db, client))
    entries = fake.playlist(res.applied[0]["spotify_id"])["entries"]
    assert EL_PASO in entries and sync.alpha_order(db, entries) == entries
    assert not in_crate(db, EL_PASO)
    # importing again doesn't pull it in either (the app's own playlists are skipped)
    import_library(client, db, progress=lambda _m: None)
    assert not in_crate(db, EL_PASO)


def test_removing_it_in_spotify_means_disliked_and_never_again(fake, client, db, setup):
    trials.add(db, night(db), ["El Paso - Marty Robbins"], client.search_tracks)
    sid = sync.apply(db, client, sync.plan(db, client)).applied[0]["spotify_id"]
    fake.playlist(sid)["entries"].remove(EL_PASO)
    sync.apply(db, client, sync.plan(db, client))
    row = trials.rows(db, night(db).identity_id)[0]
    assert row["status"] == "disliked" and row["track_json"] is None
    assert EL_PASO not in fake.playlist(sid)["entries"]
    assert not in_crate(db, EL_PASO)
    again = trials.add(db, night(db), ["El Paso - Marty Robbins"], client.search_tracks)
    assert again[0].status == "disliked_before"
    assert by_name(sync.plan(db, client))["Night Drive"].action == "unchanged"


def test_like_adds_it_to_the_crate_pinned_in_the_playlist(fake, client, db, setup):
    ident = night(db)
    trials.add(db, ident, ["El Paso - Marty Robbins"], client.search_tracks)
    sid = sync.apply(db, client, sync.plan(db, client)).applied[0]["spotify_id"]
    trials.like(db, ident, trials.find(db, ident.identity_id, "el paso"))
    assert in_crate(db, EL_PASO)
    fbk = db.conn.execute(
        "SELECT decision FROM feedback WHERE identity_id=? AND uri=?",
        (ident.identity_id, EL_PASO),
    ).fetchone()
    assert fbk["decision"] == "essential"
    # survives an import, and the next draft has it in
    import_library(client, db, progress=lambda _m: None)
    assert in_crate(db, EL_PASO)
    sc.build_drafts(db, descriptors=descriptors(db))
    sync.apply(db, client, sync.plan(db, client))
    assert EL_PASO in fake.playlist(sid)["entries"]
    # changed your mind: dislike takes it back out of the crate and the playlist
    trials.dislike(db, ident, trials.find(db, ident.identity_id, "El Paso - Marty Robbins"))
    assert not in_crate(db, EL_PASO)


def test_hearting_it_in_spotify_counts_as_like(fake, client, db, setup):
    ident = night(db)
    trials.add(db, ident, ["El Paso - Marty Robbins"], client.search_tracks)
    sync.apply(db, client, sync.plan(db, client))
    fake.lib["liked"].append({"added_at": "2026-11-01T00:00:00Z", "track": EL_PASO})
    import_library(client, db, progress=lambda _m: None)
    sync.apply(db, client, sync.plan(db, client))
    assert trials.rows(db, ident.identity_id)[0]["status"] == "liked"


def test_split_query_and_matching_never_substitutes():
    assert trials.split_query("Big Iron — Marty Robbins") == ("Big Iron", "Marty Robbins")
    assert trials.split_query("Jolene by Dolly Parton") == ("Jolene", "Dolly Parton")
    assert trials.split_query("Jolene") == ("Jolene", None)
    near = [{"type": "track", "uri": "spotify:track:x", "name": "El Pasito", "artists": []}]
    assert trials.best_match("El Paso", near) is None


def test_subtitles_are_optional_but_other_songs_never_match():
    def t(name, artist="Waylon Jennings"):
        return {
            "type": "track",
            "uri": f"spotify:track:{len(name)}",
            "name": name,
            "artists": [{"name": artist}],
        }

    full = t("Luckenbach, Texas (Back to the Basics of Love)")
    assert trials.best_match("Luckenbach, Texas - Waylon Jennings", [full]).name == full["name"]
    assert trials.best_match("Luckenbach - Waylon Jennings", [full]) is None
    assert trials.best_match("Luckenbach, Texas - Johnny Cash", [full]) is None


def test_cli_output_survives_a_cp1252_log_file(tmp_path, monkeypatch):
    import io
    import sys

    from playlist_curator import cli

    raw = io.BytesIO()
    fake_out = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", fake_out)
    cli._safe_output()
    print("100 now → +0 / -51")
    sys.stdout.flush()
    assert "→".encode() in raw.getvalue()
