import pytest

from library_builder import build_library
from playlist_curator.curator import discovery as disc
from playlist_curator.curator import identities as idn
from playlist_curator.curator import intents as it
from playlist_curator.curator import scoring as sc
from playlist_curator.storage.database import utcnow


@pytest.fixture
def lib(db):
    desc = build_library(db)
    for ref, title, kind in [
        ("U", "Indie chill", "mood"),
        ("Ñ", "Indie chill", "mood"),
        ("W", "Workout", "situation"),
        ("80s", "80s", "era"),
        ("RS", "Regular Show", "handpicked"),
    ]:
        it.set_intent(db, ref, title, kind, title)
    props, _ = disc.propose(db, descriptors=desc)
    disc.save_proposals(db, props)
    by = {p.title: p.proposal_id for p in disc.load_proposals(db)}
    for title in ("Indie chill", "Workout", "80s", "Regular Show"):
        disc.accept(db, by[title])
    return desc


def draft_by_name(result):
    return {d.identity.name: d for d in result.drafts}


def ids(bucket):
    return {s.recording_id for s in bucket}


def test_drafts_fill_each_playlist_from_its_own_definition(db, lib):
    res = sc.build_drafts(db, descriptors=lib, coverage=False)  # strong matches only
    d = draft_by_name(res)
    chill = ids(d["Indie chill"].include)
    assert {"isrc:" not in r and r for r in chill}  # ids are recording ids
    assert all("chill" in r for r in chill) and len(chill) >= 15
    # the rest of the chill songs sit just under the bar, up for review rather than lost
    near = chill | ids(d["Indie chill"].review)
    assert all("chill" in r for r in near) and len(near) >= 18
    workout = ids(d["Workout"].include)
    assert all("gym" in r for r in workout) and len(workout) >= 15
    near = workout | ids(d["Workout"].review)
    assert all("gym" in r or "jazz0" in r for r in near) and len(near) >= 18
    assert "uri:spotify:track:jazz0" not in workout  # misfit example from old W dropped
    eighties = ids(d["80s"].include)
    assert all("synth" in r for r in eighties)
    # jazz and rap have no accepted playlist, so they stay unassigned
    assert any("jazz" in r for r in res.unassigned) and any("rap" in r for r in res.unassigned)
    # any chill song not in the playlist is at least up for review there
    assert {r for r in res.unassigned if "chill" in r} <= ids(d["Indie chill"].review)


def test_reasons_and_threshold_are_reported(db, lib):
    d = draft_by_name(sc.build_drafts(db, descriptors=lib))["Workout"]
    assert sc.THRESHOLD_MIN <= d.threshold <= sc.THRESHOLD_MAX
    top = d.include[0]
    assert any(r.startswith("tags: ") for r in top.reasons)
    assert any(r.startswith("sounds like ") for r in top.reasons)
    assert top.evidence == "song"
    assert top.score >= d.threshold


def test_handpicked_holds_exactly_its_songs(db, lib):
    d = draft_by_name(sc.build_drafts(db, descriptors=lib))["Regular Show"]
    assert ids(d.include) == {"uri:spotify:track:rs0", "uri:spotify:track:synth3"}
    assert d.threshold is None and d.review == []


def test_required_avoid_years_essential_and_exclude(db, lib):
    chill = idn.get_identity(db, "Indie chill")
    # required tag nobody has -> empty except essentials
    idn.edit_identity(db, chill.identity_id, require_tags=["shoegaze"])
    now = utcnow()
    db.conn.executemany(
        "INSERT OR REPLACE INTO identity_examples VALUES (?,?,?,?,?)",
        [
            (chill.identity_id, "spotify:track:jazz5", "essential", "user", now),
            (chill.identity_id, "spotify:track:chill3", "exclude", "user", now),
        ],
    )
    d = draft_by_name(sc.build_drafts(db, descriptors=lib))["Indie chill"]
    assert ids(d.include) == {"uri:spotify:track:jazz5"}
    assert "you marked this essential" in d.include[0].reasons

    idn.edit_identity(db, chill.identity_id, unrequire_tags=["shoegaze"], avoid_tags=["indie"])
    d = draft_by_name(sc.build_drafts(db, descriptors=lib))["Indie chill"]
    assert not any("chill" in r for r in ids(d.include))  # every chill song has 'indie'

    w = idn.get_identity(db, "Workout")
    before = {
        s.recording_id: s.score
        for s in draft_by_name(sc.build_drafts(db, descriptors=lib))["Workout"].include
    }
    idn.edit_identity(db, w.identity_id, years=[1960, 1970])  # gym songs are ~2005
    after = draft_by_name(sc.build_drafts(db, descriptors=lib))["Workout"]
    s = next(x for x in after.include + after.review if x.recording_id in before)
    assert s.score == pytest.approx(before[s.recording_id] * sc.ERA_PENALTY, rel=1e-3)
    assert any("outside 1960–1970" in r for r in s.reasons)


def test_runs_are_saved_and_loadable(db, lib):
    res = sc.build_drafts(db, descriptors=lib)
    assert sc.latest_run(db) == res.run_id
    w = idn.get_identity(db, "Workout")
    rows = sc.load_draft(db, w.identity_id)
    assert rows and rows[0]["decision"] == "include" and rows[0]["reasons"]
    for _ in range(6):
        sc.build_drafts(db, descriptors=lib)
    assert db.conn.execute("SELECT COUNT(*) FROM draft_runs").fetchone()[0] == 5


def test_untagged_songs_are_never_guessed(db, lib):
    res = sc.build_drafts(db, descriptors=lib)
    assert res.untagged == 0
    lib2 = {k: v for k, v in lib.items() if k != "uri:spotify:track:chill1"}
    res = sc.build_drafts(db, descriptors=lib2)
    assert res.untagged == 1
    assert "uri:spotify:track:chill1" in res.unassigned


def test_rare_tags_count_more_than_common_ones(db, lib):
    library = sc.load_library(db, lib)
    # "indie" is on 20 songs, "dream pop" too, but "rock" (gym) and "hip hop" (rap) likewise;
    # a tag on nearly every song would get close to 1.0
    assert library.idf["saxophone"] > 1.0
    common = {f"uri:spotify:track:{g}{i}" for g in ("chill", "gym") for i in range(20)}
    vecs = {r: dict(v) for r, v in library.vectors.items()}
    for r in common:
        vecs[r]["everywhere"] = 1.0
    spec = sc.tag_specificity(vecs)
    assert spec["everywhere"] < spec["indie"]


def test_broad_definitions_are_capped(db, lib):
    broad = idn.create_identity(db, "Everything", concept="too broad")
    idn.edit_identity(
        db, broad.identity_id, add_tags={"indie": 1.0, "rock": 1.0, "jazz": 1.0, "hip hop": 1.0}
    )
    now = utcnow()
    db.conn.executemany(  # examples from four different sounds -> a very low natural bar
        "INSERT INTO identity_examples VALUES (?,?,?,?,?)",
        [
            (broad.identity_id, f"spotify:track:{g}{i}", "reference", "user", now)
            for g in ("chill", "gym", "jazz", "rap")
            for i in range(5)
        ],
    )
    d = draft_by_name(sc.build_drafts(db, descriptors=lib, coverage=False))["Everything"]
    tagged = sum(1 for v in lib.values() if v.by_source)
    assert len(d.include) <= int(sc.MAX_SHARE * tagged) + 1
    assert any("capped" in n for n in d.notes)
