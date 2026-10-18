import pytest

from library_builder import build_library
from playlist_curator.curator import feedback as fb
from playlist_curator.curator import identities as idn
from playlist_curator.curator import scoring as sc

GYM = "spotify:track:gym{}"


@pytest.fixture
def lib(db):
    desc = build_library(db)
    w = idn.create_identity(db, "Workout", concept="gym")
    idn.edit_identity(db, w.identity_id, add_tags={"rock": 1.0, "energetic": 0.8})
    chill = idn.create_identity(db, "Chill", concept="chill")
    idn.edit_identity(db, chill.identity_id, add_tags={"chill": 1.0, "indie": 0.6})
    for iid, group in ((w.identity_id, "gym"), (chill.identity_id, "chill")):
        for i in range(10):
            fb.decide_uri(db, idn.get_identity(db, iid), f"spotify:track:{group}{i}", "keep")
    sc.build_drafts(db, descriptors=desc)
    return desc


def members(db, name, lib):
    res = sc.build_drafts(db, descriptors=lib)
    d = next(x for x in res.drafts if x.identity.name == name)
    return {s.recording_id for s in d.include}, res


def test_find_song_by_title_artist_and_uri(db, lib):
    uri, label = fb.find_song(db, "Gym Song 3")
    assert uri == GYM.format(3) and label.startswith("Gym Song 3")
    assert fb.find_song(db, "Gym Song 3 - gym artist 3")[0] == GYM.format(3)
    assert fb.find_song(db, GYM.format(4))[0] == GYM.format(4)
    with pytest.raises(fb.SongNotFound):
        fb.find_song(db, "Nope Song")
    with pytest.raises(fb.AmbiguousSong):
        fb.find_song(db, "Gym Song")  # prefix of every gym song


def test_reject_only_affects_that_playlist(db, lib):
    fb.decide(db, "Workout", GYM.format(15), "reject")
    workout, _ = members(db, "Workout", lib)
    assert "uri:" + GYM.format(15) not in workout
    ex = db.conn.execute(
        "SELECT role FROM identity_examples WHERE uri=? AND identity_id=?",
        (GYM.format(15), idn.get_identity(db, "Workout").identity_id),
    ).fetchone()
    assert ex["role"] == "exclude"
    chill_id = idn.get_identity(db, "Chill").identity_id
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM identity_examples WHERE identity_id=? AND role='exclude'",
            (chill_id,),
        ).fetchone()[0]
        == 0
    )


def test_essential_beats_any_score_and_undo_restores(db, lib):
    jazz = "spotify:track:jazz2"
    fb.decide(db, "Workout", jazz, "essential")
    workout, _ = members(db, "Workout", lib)
    assert "uri:" + jazz in workout
    r = fb.undo(db, "Workout", jazz)
    assert r["restored"] == "nothing"
    workout, _ = members(db, "Workout", lib)
    assert "uri:" + jazz not in workout
    # a seed example (from an old playlist) that you reject, then undo, is an example again
    from playlist_curator.storage.database import utcnow

    wid = idn.get_identity(db, "Workout").identity_id
    db.conn.execute(
        "INSERT INTO identity_examples VALUES (?,?,'reference','seed',?)",
        (wid, GYM.format(19), utcnow()),
    )
    fb.decide(db, "Workout", GYM.format(19), "reject")
    fb.decide(db, "Workout", GYM.format(19), "keep")  # changing your mind keeps the original
    assert fb.undo(db, "Workout", GYM.format(19))["restored"] == "reference"
    with pytest.raises(fb.SongNotFound):
        fb.undo(db, "Workout", GYM.format(19))


def test_rejections_generalize_to_similar_songs(db, lib):
    w = idn.get_identity(db, "Workout")
    lib_obj = sc.load_library(db, lib)
    target = "uri:spotify:track:gym12"
    before = next(s for s in sc.score_playlist(db, w, lib_obj).include if s.recording_id == target)
    # reject some songs that are near-identical to gym12 (same group), keep the rest
    for i in (11, 13, 14):
        fb.decide(db, "Workout", GYM.format(i), "reject")
    for i in range(10):  # make the examples less similar: remove them
        fb.decide(db, "Workout", GYM.format(i), "uncertain")
    after = sc.score_playlist(db, idn.get_identity(db, "Workout"), sc.load_library(db, lib))
    s = next((x for x in after.include + after.review if x.recording_id == target), None)
    assert s is None or s.score < before.score


def test_stability_keeps_borderline_songs(db, lib, monkeypatch):
    w = idn.get_identity(db, "Workout")
    lib_obj = sc.load_library(db, lib)
    lowest = min(sc.score_playlist(db, w, lib_obj).include, key=lambda s: s.score)
    # raise the bar just above that song: new songs at its score no longer get in...
    monkeypatch.setattr(sc, "THRESHOLD_MIN", lowest.score + 0.01)
    monkeypatch.setattr(sc, "THRESHOLD_MAX", 1.0)
    fresh = sc.score_playlist(db, w, lib_obj)
    assert lowest.recording_id not in {s.recording_id for s in fresh.include}
    # ...but a song that was already in the playlist stays (lower exit bar)
    kept = sc.score_playlist(db, w, lib_obj, previous={lowest.recording_id})
    s = next(x for x in kept.include if x.recording_id == lowest.recording_id)
    assert any("kept from the last draft" in r for r in s.reasons)


def test_changes_between_runs_are_reported(db, lib):
    _, res = members(db, "Workout", lib)
    assert res.changes["Workout"] == {"added": 0, "removed": 0}
    fb.decide(db, "Workout", GYM.format(16), "reject")
    _, res = members(db, "Workout", lib)
    # the rejected song leaves; near-identical songs may follow it (rejections generalize)
    assert res.changes["Workout"]["removed"] >= 1


def test_stats_and_acceptance_rate(db, lib):
    sc.build_drafts(db, descriptors=lib)
    fb.decide(db, "Workout", GYM.format(17), "keep")  # was included
    fb.decide(db, "Workout", GYM.format(18), "reject")  # was included
    fb.decide(db, "Workout", "spotify:track:jazz3", "essential")  # manual add
    st = {s.name: s for s in fb.stats(db)}["Workout"]
    assert st.suggested_kept == 1 and st.suggested_rejected == 1
    # the 10 setup keeps happened before any draft existed, so they count as manual adds
    assert st.acceptance_rate == 0.5 and st.manual_adds == 11
    assert st.kept >= 11 and st.rejected == 1 and st.essential == 1
    rows = fb.decisions_for(db, idn.get_identity(db, "Workout").identity_id)
    assert rows[0]["decision"] in ("essential", "reject", "keep")
    log = db.conn.execute("SELECT COUNT(*) FROM feedback_log").fetchone()[0]
    assert log >= 23


def test_suggest_avoid_tags_from_rejections(db, lib):
    for i in range(4):  # reject jazz songs from Chill: they share jazz tags the examples lack
        fb.decide(db, "Chill", f"spotify:track:jazz{i}", "reject")
    sugg = {
        s["tag"] for s in fb.suggest_avoid_tags(db, idn.get_identity(db, "Chill").identity_id, lib)
    }
    assert {"jazz", "smooth jazz"} <= sugg
    assert "chill" not in sugg


def test_holdout_evaluation(db, lib):
    fb.decide(db, "Workout", "spotify:track:jazz4", "reject")
    evs = {e.name: e for e in fb.evaluate(db, lib)}
    w = evs["Workout"]
    assert w.decided == 11
    assert w.agreement is not None and w.agreement >= 0.9
    assert not w.rejected_but_would_add  # jazz isn't workout even without the rejection


def test_decide_validation(db, lib):
    with pytest.raises(ValueError):
        fb.decide(db, "Workout", GYM.format(1), "maybe")
    with pytest.raises(LookupError):
        fb.decide(db, "No such playlist", GYM.format(1), "keep")
