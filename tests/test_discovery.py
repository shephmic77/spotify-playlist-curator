import json

import numpy as np
import pytest

from library_builder import build_library
from playlist_curator.curator import discovery as disc
from playlist_curator.curator import identities as idn
from playlist_curator.curator import intents as it

DESCRIPTIONS = [
    ("U", "Indie chill", "indie chill", "mood"),
    ("Ñ", "Indie chill", "indie chill (enye)", "mood"),
    ("W", "Workout", "songs for the gym", "situation"),
    ("80s", "80s", "the 80s", "era"),
    ("N", "Car with parents", "70-80-90 songs I can play in the car with my parents", "situation"),
    ("RS", "Regular Show", "songs from Regular Show", "handpicked"),
    ("M", "Main", "oldest main playlist, a lot of songs I liked", "catch_all"),
]


@pytest.fixture
def lib(db):
    desc = build_library(db)
    for ref, title, text, kind in DESCRIPTIONS:
        it.set_intent(db, ref, text, kind, title)
    return desc


def by_title(props):
    return {p.title: p for p in props}


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("70-80-90 car songs", [1970, 1999]),
        ("the 80s", [1980, 1989]),
        ("1980s synth", [1980, 1989]),
        ("2000s pop", [2000, 2009]),
        ("workout", None),
        ("top 100 songs", None),
    ],
)
def test_years_from_text(text, years):
    assert disc.years_from_text(text) == years


def test_spherical_kmeans_and_silhouette_separate_clear_groups():
    rng = np.random.default_rng(0)
    a = rng.normal([1, 0, 0], 0.05, (30, 3))
    b = rng.normal([0, 1, 0], 0.05, (30, 3))
    x = np.vstack([a, b])
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    labels, _ = disc.spherical_kmeans(x, 2)
    assert len(set(labels[:30])) == 1 and len(set(labels[30:])) == 1
    assert labels[0] != labels[30]
    assert disc.silhouette(x, labels) > 0.8


def test_descriptions_become_concepts_from_scratch(db, lib):
    props, _ = disc.propose(db, descriptors=lib)
    t = by_title(props)

    chill = t["Indie chill"]  # U and Ñ described the same way -> one concept
    assert chill.action == "from_description"
    assert sorted(chill.playlist_ids) == ["pl_enye", "pl_u"]
    assert chill.evidence["example_songs"] == 20
    assert "indie" in chill.draft.rules["tags"]
    assert chill.draft.auto_maintain

    assert t["Car with parents"].draft.rules["years"] == [1970, 1999]
    assert t["80s"].draft.rules["years"] == [1980, 1989]
    assert t["80s"].draft.kind == "era"

    workout = t["Workout"]
    assert workout.evidence["from_playlists"][0]["misfits_left_out"] == 1
    assert "spotify:track:jazz0" not in workout.draft.reference_uris

    assert "Main" not in t  # catch-alls aren't concepts
    rs = t["Regular Show"]
    assert rs.action == "handpicked" and not rs.draft.auto_maintain
    assert set(rs.draft.reference_uris) == {"spotify:track:rs0", "spotify:track:synth3"}


def test_library_clusters_add_concepts_nobody_described(db, lib):
    props, info = disc.propose(db, descriptors=lib)
    assert info["k"] >= 6
    lib_titles = {p.title for p in props if p.action == "from_library"}
    # jazz (only in the catch-all M) and rap (R has no description) are new concepts
    assert any("Jazz" in x for x in lib_titles)
    assert any("Hip Hop" in x or "Rap" in x for x in lib_titles)
    # sounds already covered by a description are not proposed twice
    assert not any("Indie" in x or "Synthpop" in x or "80S" in x for x in lib_titles)
    chill = by_title(props)["Indie chill"]
    assert chill.evidence.get("library_clusters")


def test_save_accept_reject_and_regenerate(db, lib):
    props, _ = disc.propose(db, descriptors=lib)
    saved = disc.save_proposals(db, props)
    assert all(p.proposal_id for p in saved)
    t = by_title(disc.load_proposals(db))

    ident_id = disc.accept(db, t["Indie chill"].proposal_id, name="Late Night Indie")
    ident = idn.get_identity(db, ident_id)
    assert ident.name == "Late Night Indie" and ident.status == "approved"
    assert ident.examples == {"reference": 20}
    assert ident.origin["playlists"] == ["pl_enye", "pl_u"]

    disc.reject(db, t["Regular Show"].proposal_id)
    with pytest.raises(ValueError):
        disc.accept(db, t["Indie chill"].proposal_id)

    # Re-running propose doesn't bring back decided proposals
    props2, _ = disc.propose(db, descriptors=lib)
    disc.save_proposals(db, props2)
    pending = {p.title for p in disc.load_proposals(db)}
    assert "Indie chill" not in pending and "Regular Show" not in pending
    assert "Workout" in pending
    everything = {p.title: p.status for p in disc.load_proposals(db, include_decided=True)}
    assert everything["Regular Show"] == "rejected"

    disc.reopen(db, t["Regular Show"].proposal_id)
    assert "Regular Show" in {p.title for p in disc.load_proposals(db)}


def test_identity_editing(db, lib):
    props, _ = disc.propose(db, descriptors=lib)
    disc.save_proposals(db, props)
    p = by_title(disc.load_proposals(db))["Workout"]
    iid = disc.accept(db, p.proposal_id)
    ident = idn.edit_identity(
        db,
        iid,
        name="Gym",
        add_tags=idn.parse_tag_weights(["metal=0.7", "energetic"]),
        remove_tags=["hard rock"],
        avoid_tags=["ballad"],
        years=idn.parse_years("1990-2015"),
    )
    assert ident.name == "Gym"
    assert ident.rules["tags"]["metal"] == 0.7 and ident.rules["tags"]["energetic"] == 1.0
    assert "hard rock" not in ident.rules["tags"]
    assert ident.rules["avoid_tags"] == ["ballad"] and ident.rules["years"] == [1990, 2015]
    assert idn.get_identity(db, "gym").identity_id == iid
    ident = idn.edit_identity(db, iid, years=None, unavoid_tags=["ballad"])
    assert ident.rules["years"] is None and ident.rules["avoid_tags"] == []
    with pytest.raises(ValueError):
        idn.edit_identity(db, iid, add_tags={"x": 3.0})
    assert idn.set_status(db, iid, "retired").status == "retired"
    assert idn.list_identities(db) == []
    assert len(idn.list_identities(db, include_inactive=True)) == 1
    assert idn.example_songs(db, iid)[0]["role"] == "reference"


def test_intents_resolution_and_import(db, lib, tmp_path):
    with pytest.raises(it.PlaylistNotFound):
        it.set_intent(db, "Nope", "x", "mood")
    with pytest.raises(ValueError):
        it.set_intent(db, "U", "x", "vibes")
    assert it.resolve_playlist(db, "pl_u")["name"] == "U"
    assert it.resolve_playlist(db, "ñ")["spotify_id"] == "pl_enye"  # case-insensitive

    path = tmp_path / "intents.json"
    path.write_text(
        json.dumps(
            [
                {"playlist": "R", "title": "Rap", "intent": "rap", "kind": "sound"},
                {"playlist": "Missing", "intent": "?", "kind": "unknown"},
            ]
        )
    )
    applied, problems = it.import_intents(db, path)
    assert [a.title for a in applied] == ["Rap"]
    assert len(problems) == 1 and "Missing" in problems[0]
    assert {i.title for i in it.list_intents(db)} >= {"Rap", "Indie chill"}
    assert it.delete_intent(db, "R")


def test_ambiguous_playlist_names_are_not_guessed(db, lib):
    from library_builder import add_playlist

    add_playlist(db, "pl_h1", "H", [("spotify:track:h1", "A", "B", 2000)])
    add_playlist(db, "pl_h2", "H", [("spotify:track:h2", "C", "D", 2001)])
    with pytest.raises(it.AmbiguousPlaylist) as exc:
        it.resolve_playlist(db, "H")
    assert "pl_h1" in str(exc.value) and "pl_h2" in str(exc.value)
    assert it.resolve_playlist(db, "pl_h2")["spotify_id"] == "pl_h2"
