import json

import httpx
import pytest

from library_builder import add_playlist, build_library
from playlist_curator.curator import describe as ds
from playlist_curator.curator import identities as idn

STUDY = (
    "music to study where lyrics dont distract too much (can still have lyrics) and is chill "
    "and not too loud and overwhelming"
)


def test_offline_study_description_keeps_nuance():
    r = ds.translate_offline(STUDY)
    assert {"study", "chill", "not too loud", "lyrics ok"} <= set(r.matched_terms)
    assert r.tags["instrumental"] == 0.4  # lyrics allowed, just not distracting
    assert r.tags["lo-fi"] >= 0.7 and r.tags["mellow"] >= 0.8
    assert {"metal", "loud", "screamo"} <= set(r.avoid)
    assert "vocal" not in r.avoid
    assert r.engine == "offline"


@pytest.mark.parametrize(
    ("text", "tag", "avoided"),
    [
        ("gym", "energetic", "ambient"),
        ("chill sunday", "chill", "metal"),
        ("sleep", "ambient", "edm"),
        ("road trip", "driving", "sleep"),
    ],
)
def test_offline_common_situations(text, tag, avoided):
    r = ds.translate_offline(text)
    assert tag in r.tags and avoided in r.avoid


def test_negation_and_years():
    r = ds.translate_offline("gym music, no rap or trap, 2000s")
    assert {"rap", "trap"} <= set(r.avoid)
    assert "rap" not in r.tags and "trap" not in r.tags
    assert r.years == [2000, 2009]
    unknown = ds.translate_offline("songs that feel like a purple sunset")
    assert not unknown.tags and "ANTHROPIC_API_KEY" in unknown.note


def claude_client(reply: str, status: int = 200, seen: list | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append((dict(request.headers), body))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "bad key"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": reply}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_claude_engine_sends_only_your_text_and_validates_reply():
    seen: list = []
    reply = (
        'Here you go: {"tags": {"Lo-Fi": 1, "jazz hop": 0.9, "chillhop": 3, "": 1}, '
        '"avoid": ["metal"], "years": [1990, 2010], "note": "calm beats"}'
    )
    r = ds.translate_claude(STUDY, "sk-test", http=claude_client(reply, seen=seen))
    headers, body = seen[0]
    assert headers["x-api-key"] == "sk-test" and headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == ds.DEFAULT_MODEL
    assert body["messages"] == [{"role": "user", "content": STUDY}]  # nothing else is sent
    assert r.tags == {"lo-fi": 1.0, "jazz hop": 0.9, "chillhop": 1.0}  # clamped, cleaned
    assert r.avoid == ["metal"] and r.years == [1990, 2010]
    assert r.engine.startswith("claude")


def test_claude_engine_errors_are_clear():
    with pytest.raises(ds.DescribeError, match="ANTHROPIC_API_KEY"):
        ds.translate_claude("x", "")
    with pytest.raises(ds.DescribeError, match="401"):
        ds.translate_claude("x", "k", http=claude_client("", status=401))
    with pytest.raises(ds.DescribeError, match="recipe"):
        ds.translate_claude("x", "k", http=claude_client("sorry, no"))
    with pytest.raises(ds.DescribeError, match="no usable tags"):
        ds.translate_claude("x", "k", http=claude_client('{"tags": {}}'))


def test_auto_engine_falls_back_to_offline_without_key():
    assert ds.translate("study", engine="auto").engine == "offline"


def test_resolve_examples_by_artist_and_fuzzy_title(db):
    desc = build_library(db)
    add_playlist(
        db,
        "pl_v",
        "V",
        [
            ("spotify:track:luv3", "Luv(sic) Part 3", "Nujabes", 2003),
            ("spotify:track:aruarian", "Aruarian Dance", "Nujabes", 2004),
            ("spotify:track:other", "Feather", "Nujabes, Cise Starr", 2005),
        ],
    )
    m = {
        x.query: x
        for x in ds.resolve_examples(
            db, ["Nujabes", "luvsic", "aruarain dance", "nothing like this"]
        )
    }
    assert m["Nujabes"].kind == "artist" and len(m["Nujabes"].uris) == 3
    assert m["luvsic"].kind == "song" and m["luvsic"].uris == ["spotify:track:luv3"]
    assert m["aruarain dance"].uris == ["spotify:track:aruarian"]  # typo tolerated
    assert m["nothing like this"].kind == "none"
    assert desc  # library has tags; example_tags returns [] for these untagged songs
    assert ds.example_tags(m["Nujabes"].uris, db, desc) == []


def test_example_tags_and_grounding(db):
    desc = build_library(db)
    uris = [f"spotify:track:chill{i}" for i in range(5)]
    tags = dict(ds.example_tags(uris, db, desc))
    assert tags["indie"] == 1.0 and tags["chill"] == 1.0
    recipe = ds.translate_offline("chill")
    reports = {r.tag: r for r in ds.ground(recipe, desc)}
    assert reports["chill"].songs >= 20
    assert reports["chillout"].songs == 0
    assert any(s[0] == "chill" for s in reports["chillout"].suggestions)


def test_apply_recipe_creates_playlist_with_examples(db):
    build_library(db)
    recipe = ds.translate_offline(STUDY)
    iid = ds.apply_recipe(
        db, recipe, create="Study", example_uris=["spotify:track:chill1", "spotify:track:chill2"]
    )
    ident = idn.get_identity(db, iid)
    assert ident.concept == STUDY and ident.rules["tags"]["instrumental"] == 0.4
    assert "metal" in ident.rules["avoid_tags"]
    assert ident.examples == {"reference": 2}
    ds.apply_recipe(db, ds.translate_offline("gym"), apply_to="Study")
    assert "energetic" in idn.get_identity(db, iid).rules["tags"]
