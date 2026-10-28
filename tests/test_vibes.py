"""Songs are judged as individuals, by their own sound; every song finds a home."""

import pytest

from library_builder import add_playlist, descriptors_for
from playlist_curator.curator import describe as ds
from playlist_curator.curator import feedback as fb
from playlist_curator.curator import identities as idn
from playlist_curator.curator import scoring as sc
from playlist_curator.metadata import vibes
from playlist_curator.metadata.descriptors import Descriptors
from playlist_curator.storage.database import utcnow

C = vibes.TagClasses()

# What taggers really write for MF DOOM's "Lavender Buds": artist genre words on the song
LAVENDER_TRACK = {"hip hop": 1.0, "jazz hop": 0.7, "chill": 0.6, "instrumental hip hop": 0.5}
LAVENDER_ARTIST = {"rap": 1.0, "underground hip hop": 0.9, "hip hop": 0.9}


def test_tag_classes():
    assert C.of("chill") == C.of("chillout") == C.of("mellow") == "vibe"
    assert C.of("hip hop") == C.of("rap") == C.of("underground hip hop") == "genre"
    assert C.of("80s") == C.of("oldies") == "era"
    assert C.of("british") == C.of("2011") == C.of("live") == "ignore"
    assert C.of("jazz hop") == "genre" and vibes.SOUND_OF["jazz hop"]["jazzy"] == 1.0
    assert C.of("yacht rock") == "genre"  # pattern
    assert vibes.default_class("glorptastic") == "other"
    assert C.of("sparkly") == "vibe" and vibes.VIBE_ALIASES["sparkly"] == "bright"
    # a much bigger vocabulary describes the song itself
    for word, canon in [
        ("falsetto", "falsetto"),
        ("harmonies", "harmonies"),
        ("fuzzy", "fuzzy"),
        ("808s", "bass"),
        ("guitar riffs", "riffs"),
        ("golden hour", "sunset"),
        ("wistful", "bittersweet"),
        ("bouncy", "bouncy"),
        ("storytelling", "storytelling"),
    ]:
        assert C.of(word) == "vibe" and vibes.VIBE_ALIASES[word] == canon
    assert vibes.SOUND_OF["boom bap"]["drums"] > 0 and vibes.SOUND_OF["yacht rock"]["smooth"]


def test_a_song_is_judged_by_its_own_sound_not_its_artist():
    p = vibes.profile({"lastfm_track": LAVENDER_TRACK, "lastfm_artist": LAVENDER_ARTIST}, C)
    assert p.evidence == "song"
    assert p.vibe["chill"] == 0.6
    assert p.vibe["jazzy"] > 0.3 and "lo-fi" in p.vibe  # from "jazz hop"
    assert "rap" not in p.vibe and "hip hop" not in p.vibe
    assert "rap" not in p.labels  # the artist's tags aren't used at all
    assert p.labels["hip hop"] == 1.0  # kept, but only counts where a playlist asks for it


def test_artist_tags_are_only_a_flagged_last_resort():
    p = vibes.profile({"lastfm_artist": {"chill": 1.0, "rap": 1.0}}, C)
    assert p.evidence == "artist" and p.vibe["chill"] == vibes.ARTIST_FALLBACK
    assert vibes.profile({}, C).evidence == "none"


def test_audio_measures_count_as_the_songs_own():
    p = vibes.profile({"acousticbrainz": {"relaxed": 0.5, "instrumental": 0.6}}, C)
    assert p.evidence == "song" and p.vibe["relaxed"] == 0.5 and p.vibe["instrumental"] == 0.6


def test_your_tag_choices_win(db):
    vibes.set_class(db, "Indie", "vibe")
    vibes.set_class(db, "chill", "ignore")
    classes = vibes.load_classes(db)
    p = vibes.profile({"lastfm_track": {"indie": 1.0, "chill": 1.0}}, classes)
    assert p.vibe == {"indie": 1.0} and p.labels == {}
    vibes.set_class(db, "indie", None)
    assert vibes.load_classes(db).of("indie") == "genre"
    with pytest.raises(ValueError):
        vibes.set_class(db, "x", "mood")


def test_descriptions_use_sound_words_unless_you_name_a_genre_or_era():
    gym = ds.translate("gym, high energy", engine="offline")
    assert "rap" not in gym.tags and "metal" not in gym.tags
    assert "heavy" in gym.tags and "energetic" in gym.tags
    study = ds.translate("music to study to", engine="offline")
    assert "rap" not in study.avoid  # Lavender Buds can still be study music
    eighties = ds.translate("80s synth songs for driving", engine="offline")
    assert "80s" in eighties.tags
    rap = ds.translate("rap for the gym", engine="offline")
    assert "rap" in rap.tags


@pytest.fixture
def lib(db):
    songs = [
        ("spotify:track:lav", "Lavender Buds", "MF DOOM", 2003),
        ("spotify:track:rap1", "Hard Bars", "Rapper A", 2016),
        ("spotify:track:rap2", "More Bars", "Rapper B", 2017),
        ("spotify:track:chill1", "Soft Morning", "Band C", 2015),
        ("spotify:track:chill2", "Slow Tide", "Band D", 2018),
        ("spotify:track:funk1", "Groove Thing", "Band E", 1978),
        ("spotify:track:odd", "Odd One", "Band F", 2010),
        ("spotify:track:none", "No Data", "Band G", 2010),
    ]
    add_playlist(db, "pl_all", "All", songs)
    tags = {
        "spotify:track:lav": LAVENDER_TRACK,
        "spotify:track:rap1": {"rap": 1.0, "hip hop": 1.0, "aggressive": 0.8},
        "spotify:track:rap2": {"rap": 1.0, "hip hop": 0.9, "aggressive": 0.7},
        "spotify:track:chill1": {"chill": 1.0, "mellow": 0.8},
        "spotify:track:chill2": {"chill": 0.9, "mellow": 0.9, "jazzy": 0.3},
        "spotify:track:funk1": {"funk": 1.0, "groovy": 0.8},
        "spotify:track:odd": {"sparkly": 1.0, "groovy": 0.2},
    }
    return descriptors_for(db, tags)


def _example(db, ident, uri, role="reference"):
    db.conn.execute(
        "INSERT INTO identity_examples VALUES (?,?,?,'user',?)",
        (ident.identity_id, uri, role, utcnow()),
    )


def _drafts(db, lib, **kw):
    # these tests are about leftover placement and generous bars, so ask for them explicitly
    kw.setdefault("coverage", True)
    kw.setdefault("style", "inclusive")
    res = sc.build_drafts(db, descriptors=lib, **kw)
    return res, {d.identity.name: d for d in res.drafts}


def _uris(db, d):
    rec = {r[0]: r[1] for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings")}
    return {rec[s.recording_id] for s in d.include}


def test_lavender_buds_goes_with_chill_songs_not_rap(db, lib):
    chill = idn.create_identity(db, "Chill")
    idn.edit_identity(db, chill.identity_id, add_tags={"chill": 1.0, "mellow": 0.6})
    for u in ("spotify:track:chill1", "spotify:track:chill2"):
        _example(db, chill, u)
    rap = idn.create_identity(db, "Rap")
    idn.edit_identity(db, rap.identity_id, add_tags={"rap": 1.0, "aggressive": 0.5})
    for u in ("spotify:track:rap1", "spotify:track:rap2"):
        _example(db, rap, u)
    _res, d = _drafts(db, lib)
    assert "spotify:track:lav" in _uris(db, d["Chill"])
    assert "spotify:track:lav" not in _uris(db, d["Rap"])
    rec = {r[1]: r[0] for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings")}
    why = next(x for x in d["Chill"].include if x.recording_id == rec["spotify:track:lav"])
    assert "chill" in " ".join(why.reasons) and "hip hop" not in " ".join(why.reasons)


def test_genre_counts_only_where_a_playlist_asks_for_it(db, lib):
    vibe_only = idn.create_identity(db, "Aggressive")
    idn.edit_identity(db, vibe_only.identity_id, add_tags={"aggressive": 1.0})
    _example(db, vibe_only, "spotify:track:rap1")
    _res, d = _drafts(db, lib, coverage=False)
    s = d["Aggressive"].scores
    rec = {r[1]: r[0] for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings")}
    # Lavender Buds shares the "hip hop" label with the example, but the label doesn't count
    # here, so it isn't "similar" to the rap example
    assert (s[rec["spotify:track:lav"]].example_score or 0) < 0.2


def test_every_song_gets_a_home_except_songs_with_no_data(db, lib):
    chill = idn.create_identity(db, "Chill")
    idn.edit_identity(db, chill.identity_id, add_tags={"chill": 1.0})
    funky = idn.create_identity(db, "Funky")
    idn.edit_identity(db, funky.identity_id, add_tags={"funky": 1.0, "groovy": 1.0})
    hand = idn.create_identity(db, "Mine", kind="handpicked", auto_maintain=False)
    _example(db, hand, "spotify:track:chill1")
    res, d = _drafts(db, lib)
    placed = _uris(db, d["Chill"]) | _uris(db, d["Funky"])
    assert "spotify:track:odd" in _uris(db, d["Funky"])  # weak "groovy" match
    weak = next(s for s in d["Funky"].include if s.reasons[0] == sc.WEAK_FIT)
    assert weak and d["Funky"].weak >= 1
    assert _uris(db, d["Mine"]) == {"spotify:track:chill1"}  # hand-picked never gets extras
    rec = {r[0]: r[1] for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings")}
    left = {rec[r] for r in res.unassigned}
    assert "spotify:track:none" in left  # nothing known about it
    # rap songs have only genre labels nobody asked for and no vibe in common: unplaced
    assert placed | left | _uris(db, d["Mine"]) >= {"spotify:track:lav", "spotify:track:odd"}

    # never placed where you rejected it
    fb.decide_uri(db, idn.get_identity(db, "Funky"), "spotify:track:odd", "reject")
    _res, d = _drafts(db, lib)
    assert "spotify:track:odd" not in _uris(db, d["Funky"])

    sc.set_coverage(db, False)
    _res, d = _drafts(db, lib)
    assert d["Funky"].weak == 0


def test_descriptors_of_a_song_without_song_data():
    d = Descriptors("r", by_source={"mb_artist": {"chill": 1.0}})
    assert vibes.profile(d.by_source, C).evidence == "artist"


def test_descriptions_read_every_word_you_type():
    rap = ds.translate("rap, hip hop", engine="offline")
    assert {"rap", "hip hop"} <= set(rap.tags)
    alt = ds.translate("90s and 2000s rock, alternative rock, grunge", engine="offline")
    assert {"90s", "00s", "alternative rock", "grunge"} <= set(alt.tags)
    riffs = ds.translate("guitar, drums and bass stand out, energetic", engine="offline")
    assert {"guitar", "drums", "bass"} <= set(riffs.tags)
    assert "acoustic" not in riffs.tags and "bass" not in riffs.avoid
    groove = ds.translate("soul, funk and jazz, groovy", engine="offline")
    assert {"soul", "funk", "jazz", "funky", "jazzy", "soulful"} <= set(groove.tags)
    study = ds.translate("study, lyrics ok but not distracting, not loud", engine="offline")
    assert "lyrical" not in study.tags and "loud" in study.avoid
    assert "video game music" not in study.tags  # "music" alone isn't asking for it
    free = ds.translate("bouncy falsetto, windows down", engine="offline")
    assert {"bouncy", "falsetto", "driving"} <= set(free.tags)
    assert vibes.words_in("late night drive") == {"late night": 0.9, "driving": 0.9}
    assert vibes.labels_in("no rap please") == {"rap": 0.9}  # negation handled by describe
    assert "rap" not in ds.translate("chill, no rap", engine="offline").tags


def test_junk_tags_are_ignored():
    junk = {"wsum 91.7 fm madison": 0.5, "cityscape with dj bean": 0.5, "glorptastic": 1.0}
    for t in junk:
        assert C.of(t) in ("ignore", "other")
    assert C.of("wsum 91.7 fm madison") == C.of("cityscape with dj bean") == "ignore"
    p = vibes.profile({"lastfm_track": junk, "acousticbrainz": {"relaxed": 0.6}}, C)
    assert (
        p.vibe["relaxed"] == 0.6
        and not ({"wsum 91.7 fm madison"} & set(p.vibe))
        and p.evidence == "song"
    )
    # junk-only song tags don't count as "song data": falls back to the artist, flagged
    p = vibes.profile({"lastfm_track": junk, "lastfm_artist": {"chill": 1.0}}, C)
    assert p.evidence == "artist"


def test_related_words_let_descriptions_and_data_meet():
    # AcousticBrainz says "relaxed" and "party"; people say "chill" and "energetic"
    p = vibes.profile({"acousticbrainz": {"relaxed": 0.7, "party": 0.5}}, C)
    assert p.vibe["chill"] == round(0.7 * 0.8, 4) and p.vibe["calm"] > 0
    assert p.vibe["energetic"] == round(0.5 * 0.6, 4)
    # one step only, and a word the song carries itself is never lowered
    p = vibes.profile({"lastfm_track": {"chill": 0.9, "relaxed": 0.2}}, C)
    assert p.vibe["chill"] == 0.9 and p.vibe["relaxed"] == round(0.9 * 0.8, 4)


def test_leftovers_go_where_they_rank_highest_not_to_the_broadest_playlist(db, lib):
    broad = idn.create_identity(db, "Broad")
    idn.edit_identity(
        db,
        broad.identity_id,
        add_tags={t: 1.0 for t in ("chill", "mellow", "groovy", "aggressive", "jazzy", "lo-fi")},
    )
    funky = idn.create_identity(db, "Funky")
    idn.edit_identity(db, funky.identity_id, add_tags={"funky": 1.0, "groovy": 1.0})
    _res, d = _drafts(db, lib)
    # "Odd One" is only faintly groovy: it ranks near the top of Funky's candidates but low
    # among Broad's, so it goes to Funky even though Broad's long list also touches it
    assert "spotify:track:odd" in _uris(db, d["Funky"])


def test_playlists_pick_their_own_examples_when_you_give_none(db, lib):
    chill = idn.create_identity(db, "Chill")
    idn.edit_identity(db, chill.identity_id, add_tags={"chill": 1.0, "mellow": 0.8})
    rec = {r[1]: r[0] for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings")}
    from playlist_curator.curator.scoring import load_library, pick_auto_examples

    library = load_library(db, lib)
    auto = pick_auto_examples(db, idn.get_identity(db, "Chill"), library)
    assert {rec["spotify:track:chill1"], rec["spotify:track:chill2"]} <= auto
    assert rec["spotify:track:rap1"] not in auto
    _res, d = _drafts(db, lib)
    assert any("picked automatically" in n for n in d["Chill"].notes)
    assert not any("default threshold" in n for n in d["Chill"].notes)  # enough examples now

    # never a song you rejected there; your own examples come first
    fb.decide_uri(db, idn.get_identity(db, "Chill"), "spotify:track:chill1", "reject")
    assert rec["spotify:track:chill1"] not in pick_auto_examples(
        db, idn.get_identity(db, "Chill"), load_library(db, lib)
    )
    for u in ("spotify:track:chill2", "spotify:track:lav", "spotify:track:funk1"):
        _example(db, idn.get_identity(db, "Chill"), u)
    assert pick_auto_examples(db, idn.get_identity(db, "Chill"), load_library(db, lib)) == set()


def test_auto_examples_are_one_per_artist(db):
    songs = [
        (f"spotify:track:s{i}", f"Song {i}", "Same Band" if i < 4 else f"Band {i}", 2010)
        for i in range(8)
    ]
    add_playlist(db, "pl", "P", songs)
    desc = descriptors_for(db, {u: {"chill": 1.0} for u, *_ in songs})
    ident = idn.create_identity(db, "Chill")
    idn.edit_identity(db, ident.identity_id, add_tags={"chill": 1.0})
    from playlist_curator.curator.scoring import load_library, pick_auto_examples

    lib2 = load_library(db, desc)
    picked = pick_auto_examples(db, idn.get_identity(db, "Chill"), lib2)
    artists = [lib2.titles[r].split(" — ")[-1] for r in picked]
    assert len(picked) == 5 and len(set(artists)) == 5


def test_a_name_can_only_be_created_once_and_duplicates_can_be_fixed(db, lib):
    idn.create_identity(db, "Chill")
    with pytest.raises(ValueError, match="already have a playlist called 'Chill'"):
        idn.create_identity(db, "chill")
    # duplicates made before this check existed
    db.conn.execute(
        "INSERT INTO identities (name, kind, concept, status, auto_maintain, rules_json, "
        "origin_json, created_at, updated_at) SELECT name, kind, concept, status, auto_maintain,"
        " rules_json, origin_json, created_at, updated_at FROM identities WHERE name='Chill'"
    )
    copy = max(r[0] for r in db.conn.execute("SELECT identity_id FROM identities"))
    _example(db, idn.get_identity(db, copy), "spotify:track:chill1", "essential")
    out = idn.retire_duplicates(db)
    assert out == [("Chill", 1, [copy])]
    assert [i.name for i in idn.list_identities(db)] == ["Chill"]
    assert idn.get_identity(db, "Chill").identity_id == 1  # retired copy doesn't clash
    roles = db.conn.execute("SELECT role FROM identity_examples WHERE identity_id=1").fetchall()
    assert [r[0] for r in roles] == ["essential"]  # your marks move to the kept one
    assert idn.retire_duplicates(db) == []


def test_coverage_tries_but_never_forces_a_song_you_removed(db, lib):
    chill = idn.create_identity(db, "Chill")
    idn.edit_identity(db, chill.identity_id, add_tags={"chill": 1.0})
    funky = idn.create_identity(db, "Funky")
    idn.edit_identity(db, funky.identity_id, add_tags={"funky": 1.0, "groovy": 1.0})
    _res, d = _drafts(db, lib)
    assert "spotify:track:odd" in _uris(db, d["Funky"])  # a weak fit, placed
    # you removed it from Chill (where it never was): it must not be forced into Funky either
    fb.decide_uri(db, idn.get_identity(db, "Chill"), "spotify:track:odd", "reject")
    _res, d = _drafts(db, lib)
    assert "spotify:track:odd" not in _uris(db, d["Funky"]) | _uris(db, d["Chill"])


def test_redefine_replaces_words_but_keeps_your_decisions(db, lib):
    r1 = ds.translate("chill, mellow", engine="offline")
    iid = ds.apply_recipe(db, r1, create="Groove")
    fb.decide_uri(db, idn.get_identity(db, iid), "spotify:track:chill1", "reject")
    r2 = ds.translate("upbeat, groovy, head bobbing, not slow, not calm", engine="offline")
    ds.apply_recipe(db, r2, apply_to="Groove", replace=True)
    g = idn.get_identity(db, "Groove")
    assert "groovy" in g.rules["tags"] and "mellow" not in g.rules["tags"]
    assert {"slow", "calm"} <= set(g.rules["avoid_tags"])
    assert g.concept.startswith("upbeat")
    decisions = {d["uri"]: d["decision"] for d in fb.decisions_for(db, iid)}
    assert decisions["spotify:track:chill1"] == "reject"


def test_artist_examples_only_bring_their_songs_that_fit(db, lib):
    recipe = ds.translate("rap, aggressive", engine="offline")
    rec = {r[1]: r[0] for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings")}
    uris = ["spotify:track:rap1", "spotify:track:chill1", "spotify:track:lav"]
    fit = ds.songs_that_fit(uris, recipe, db, lib)
    assert "spotify:track:rap1" in fit and "spotify:track:chill1" not in fit
    assert rec  # sanity


def test_a_genre_label_alone_doesnt_make_an_artist_song_fit(db, lib):
    recipe = ds.translate("hip hop, aggressive", engine="offline")
    # Lavender Buds carries "hip hop" but nothing aggressive: not "their songs like this"
    fit = ds.songs_that_fit(["spotify:track:lav", "spotify:track:rap1"], recipe, db, lib)
    assert fit == ["spotify:track:rap1"]


def test_your_named_examples_are_always_in(db, lib):
    ident = idn.create_identity(db, "Picky")
    idn.edit_identity(db, ident.identity_id, add_tags={"aggressive": 1.0})
    _example(db, ident, "spotify:track:chill1")  # an example that doesn't match the words
    _res, d = _drafts(db, lib, style="strict", coverage=False)
    assert "spotify:track:chill1" in _uris(db, d["Picky"])
    why = next(s for s in d["Picky"].include if s.reasons[0] == "your example")
    assert why
