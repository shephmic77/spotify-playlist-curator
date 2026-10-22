"""Turn your own words ("study", "gym", "late night drive, no screaming") into tag rules.

Two engines:
- **offline** (always available): a built-in dictionary of common situations and moods,
  mapped to community tags that Last.fm / MusicBrainz use.
- **claude** (optional, needs ``ANTHROPIC_API_KEY``): sends *only the text you typed* to
  Claude and asks for the same kind of recipe. No song, artist, playlist or other library
  data is ever included in the request (Spotify's terms forbid feeding Spotify content to an
  AI model, and this keeps your library private too).

Either way, the result is just a recipe: tags with weights, tags to avoid, an optional year
range. Matching songs to it happens locally and transparently (``scoring.py``).
``ground`` then checks the recipe against your library's tags so you can see which tags
actually match songs, and suggests close tags your library does use.
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from playlist_curator.curator.discovery import years_from_text
from playlist_curator.metadata import vibes
from playlist_curator.metadata.descriptors import Descriptors

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-5-5"

# term(s) -> recipe. Tags are common Last.fm/MusicBrainz community tags.
LEXICON: dict[tuple[str, ...], dict[str, Any]] = {
    ("study", "studying", "focus", "homework", "reading", "concentrate", "work"): {
        "tags": {
            "instrumental": 1.0,
            "ambient": 0.9,
            "lo-fi": 0.9,
            "chillhop": 0.8,
            "jazz hop": 0.8,
            "piano": 0.7,
            "classical": 0.6,
            "downtempo": 0.6,
            "post-rock": 0.5,
            "soundtrack": 0.5,
            "video game music": 0.5,
        },
        "avoid": ["metal", "hardcore", "screamo", "rap", "party"],
    },
    ("gym", "workout", "lifting", "lift", "training", "running", "run", "cardio", "hype", "pump"): {
        "tags": {
            "energetic": 1.0,
            "hard rock": 0.8,
            "metal": 0.7,
            "nu metal": 0.7,
            "rap": 0.7,
            "hip hop": 0.6,
            "trap": 0.6,
            "edm": 0.7,
            "electronic": 0.5,
            "dubstep": 0.6,
            "drum and bass": 0.6,
            "punk": 0.6,
            "rock": 0.5,
        },
        "avoid": ["ambient", "sleep", "mellow", "acoustic", "ballad", "lullaby"],
    },
    ("chill", "relax", "relaxing", "calm", "mellow", "laid back", "lazy", "sunday"): {
        "tags": {
            "chill": 1.0,
            "chillout": 0.9,
            "mellow": 0.9,
            "relaxing": 0.9,
            "downtempo": 0.7,
            "lo-fi": 0.7,
            "indie": 0.4,
            "acoustic": 0.5,
            "dream pop": 0.6,
            "soft rock": 0.5,
            "bedroom pop": 0.5,
        },
        "avoid": ["metal", "hardcore", "screamo", "aggressive"],
    },
    ("sleep", "sleeping", "bedtime", "night time", "wind down"): {
        "tags": {
            "ambient": 1.0,
            "sleep": 1.0,
            "piano": 0.8,
            "classical": 0.7,
            "calm": 0.8,
            "relaxing": 0.8,
            "drone": 0.5,
            "new age": 0.5,
        },
        "avoid": ["energetic", "metal", "rap", "edm", "dance", "rock"],
    },
    ("party", "pregame", "club", "dance", "dancing"): {
        "tags": {
            "dance": 1.0,
            "party": 1.0,
            "edm": 0.8,
            "house": 0.8,
            "pop": 0.6,
            "electronic": 0.6,
            "hip hop": 0.6,
            "disco": 0.6,
            "funk": 0.5,
        },
        "avoid": ["sad", "ambient", "sleep", "melancholic"],
    },
    ("drive", "driving", "road trip", "roadtrip", "car", "cruise", "cruising", "highway"): {
        "tags": {
            "driving": 1.0,
            "classic rock": 0.8,
            "rock": 0.6,
            "synthwave": 0.7,
            "indie rock": 0.6,
            "alternative rock": 0.5,
            "summer": 0.5,
            "80s": 0.4,
        },
        "avoid": ["sleep", "ambient"],
    },
    ("sad", "melancholy", "melancholic", "heartbreak", "crying", "rainy", "rain", "blue"): {
        "tags": {
            "sad": 1.0,
            "melancholic": 0.9,
            "melancholy": 0.9,
            "emotional": 0.7,
            "slowcore": 0.6,
            "indie folk": 0.5,
            "singer songwriter": 0.5,
            "rainy": 0.6,
        },
        "avoid": ["party", "happy", "dance"],
    },
    ("happy", "upbeat", "feel good", "feelgood", "sunny", "good vibes", "positive"): {
        "tags": {
            "happy": 1.0,
            "feel good": 1.0,
            "upbeat": 0.9,
            "summer": 0.7,
            "pop": 0.5,
            "funk": 0.5,
            "soul": 0.4,
            "indie pop": 0.5,
        },
        "avoid": ["sad", "dark", "melancholic"],
    },
    ("summer", "beach", "surf", "sun", "vacation", "tropical"): {
        "tags": {
            "summer": 1.0,
            "surf": 0.8,
            "surf rock": 0.8,
            "beach": 0.8,
            "reggae": 0.5,
            "tropical": 0.6,
            "indie pop": 0.5,
            "feel good": 0.6,
        },
        "avoid": ["dark", "winter"],
    },
    ("late night", "night", "midnight", "nocturnal", "3am"): {
        "tags": {
            "night": 0.9,
            "late night": 1.0,
            "synthwave": 0.7,
            "downtempo": 0.6,
            "dream pop": 0.6,
            "chillwave": 0.6,
            "trip hop": 0.6,
            "atmospheric": 0.6,
        },
        "avoid": ["party", "happy"],
    },
    ("morning", "coffee", "breakfast", "wake up"): {
        "tags": {
            "acoustic": 0.8,
            "indie folk": 0.7,
            "folk": 0.6,
            "jazz": 0.5,
            "soft rock": 0.5,
            "mellow": 0.6,
            "happy": 0.4,
        },
        "avoid": ["metal", "aggressive"],
    },
    ("romantic", "love", "date", "dinner"): {
        "tags": {
            "love": 1.0,
            "romantic": 1.0,
            "soul": 0.7,
            "r&b": 0.7,
            "jazz": 0.5,
            "smooth jazz": 0.5,
            "ballad": 0.5,
        },
        "avoid": ["metal", "aggressive", "angry"],
    },
    ("gaming", "game", "games", "video game"): {
        "tags": {
            "video game music": 1.0,
            "soundtrack": 0.8,
            "chiptune": 0.7,
            "electronic": 0.5,
            "ambient": 0.4,
            "synthwave": 0.5,
        },
        "avoid": [],
    },
    ("angry", "aggressive", "rage", "heavy", "heavier", "mosh"): {
        "tags": {
            "metal": 1.0,
            "hardcore": 0.8,
            "aggressive": 0.9,
            "nu metal": 0.8,
            "metalcore": 0.8,
            "hard rock": 0.7,
            "punk": 0.6,
            "heavy metal": 0.8,
        },
        "avoid": ["mellow", "acoustic", "ambient", "sleep"],
    },
    ("nostalgic", "nostalgia", "throwback", "oldies", "retro"): {
        "tags": {
            "nostalgic": 1.0,
            "oldies": 0.9,
            "classic rock": 0.6,
            "80s": 0.5,
            "90s": 0.5,
            "70s": 0.5,
            "60s": 0.5,
        },
        "avoid": [],
    },
    ("acoustic", "unplugged", "guitar"): {
        "tags": {
            "acoustic": 1.0,
            "singer songwriter": 0.7,
            "folk": 0.7,
            "indie folk": 0.6,
            "guitar": 0.8,
        },
        "avoid": ["edm", "dubstep", "electronic"],
    },
    ("instrumental", "no lyrics", "no vocals", "without lyrics"): {
        "tags": {
            "instrumental": 1.0,
            "post-rock": 0.6,
            "classical": 0.6,
            "ambient": 0.6,
            "soundtrack": 0.6,
            "jazz": 0.4,
        },
        "avoid": ["rap", "vocal"],
    },
    (
        "not too loud",
        "not loud",
        "quiet",
        "soft",
        "gentle",
        "not overwhelming",
        "not too overwhelming",
        "not intense",
        "low key",
        "lowkey",
    ): {
        "tags": {"mellow": 0.9, "soft": 0.8, "calm": 0.8, "relaxing": 0.7, "chill": 0.6},
        "avoid": ["loud", "noise", "aggressive", "metal", "hardcore", "screamo", "intense"],
    },
    ("cooking", "cleaning", "chores"): {
        "tags": {
            "funk": 0.7,
            "soul": 0.7,
            "disco": 0.6,
            "feel good": 0.8,
            "pop": 0.5,
            "motown": 0.6,
            "upbeat": 0.7,
        },
        "avoid": ["sad", "ambient"],
    },
}
# "no X" / "without X" / "not X" in your text becomes an avoided tag
_NEGATION = re.compile(
    r"\b(?:no|without|not|avoid|except)\s+(?:too\s+|very\s+|so\s+)?"
    r"([a-z0-9&' -]+?)(?=[,.;()]|$| and | but )"
)
# "lyrics are fine", "can still have lyrics", "lyrics don't distract": don't force instrumental
_LYRICS_OK = re.compile(
    r"can (still )?have (lyrics|vocals)|lyrics (are |is )?(ok|okay|fine|allowed)"
    r"|vocals (are |is )?(ok|okay|fine|allowed)|(lyrics|vocals) (dont|don't|do not|that don'?t)"
    r" distract|not distracting"
)


@dataclass
class Recipe:
    text: str
    engine: str
    tags: dict[str, float] = field(default_factory=dict)
    avoid: list[str] = field(default_factory=list)
    years: list[int] | None = None
    matched_terms: list[str] = field(default_factory=list)
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


class DescribeError(RuntimeError):
    pass


def _add(recipe: Recipe, tags: dict[str, float], avoid: list[str]) -> None:
    for t, w in tags.items():
        recipe.tags[t] = max(recipe.tags.get(t, 0.0), w)
    for t in avoid:
        if t not in recipe.avoid:
            recipe.avoid.append(t)


def translate_offline(text: str) -> Recipe:
    recipe = Recipe(text=text, engine="offline")
    low = " " + re.sub(r"[^a-z0-9&' ]+", " ", text.lower()) + " "
    negated = {m.group(1).strip() for m in _NEGATION.finditer(text.lower())}
    for terms, entry in LEXICON.items():
        hit = next(
            (t for t in terms if f" {t} " in low and (t not in negated or t.startswith("not "))),
            None,
        )
        if hit:
            recipe.matched_terms.append(hit)
            _add(recipe, entry["tags"], entry["avoid"])
    for neg in negated:  # "no screaming", "without rap"
        for word in re.split(r"\s+or\s+|\s*/\s*", neg):
            word = word.strip()
            if word and word not in recipe.avoid:
                recipe.avoid.append(word)
            recipe.tags.pop(word, None)
    if _LYRICS_OK.search(text.lower()):
        # Lyrics are allowed, just not distracting: soften "instrumental", don't rule out vocals
        if "instrumental" in recipe.tags:
            recipe.tags["instrumental"] = min(recipe.tags["instrumental"], 0.4)
        recipe.avoid = [a for a in recipe.avoid if a not in ("vocal", "lyrics", "vocals")]
        recipe.matched_terms.append("lyrics ok")
    recipe.tags = {t: w for t, w in recipe.tags.items() if t not in recipe.avoid}
    recipe.years = years_from_text(text)
    if not recipe.matched_terms:
        recipe.note = (
            "None of the built-in situations matched. Add tags yourself, or set "
            "ANTHROPIC_API_KEY to translate free-form descriptions with Claude."
        )
    return recipe


_SYSTEM = (
    "You translate a person's description of a playlist into music tags of the kind used on "
    "Last.fm and MusicBrainz. Songs are judged by how they sound, not by genre or era, so use "
    "lowercase sound, mood, energy, texture, instrument or situation words (such as 'chill', "
    "'mellow', 'funky', 'groovy', 'dreamy', 'energetic', 'lo-fi', 'instrumental', 'piano', "
    "'female vocals', 'late night'). Only use a genre or era word (like 'rap' or '80s') if "
    "the person named that genre or era. Reply with JSON "
    'only: {"tags": {"tag": weight 0.1-1.0, ...}, "avoid": ["tag", ...], '
    '"years": [from, to] or null, "note": "one short sentence"}. Use 5-15 tags, most important '
    "highest. Put things the person rules out in avoid. Never name specific songs or artists."
)


def translate_claude(
    text: str, api_key: str, *, model: str = DEFAULT_MODEL, http: httpx.Client | None = None
) -> Recipe:
    """Ask Claude for a recipe. Only ``text`` (what you typed) is sent."""
    if not api_key:
        raise DescribeError("ANTHROPIC_API_KEY is not set in .env")
    client = http or httpx.Client(timeout=60.0)
    try:
        resp = client.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 600,
                "system": _SYSTEM,
                "messages": [{"role": "user", "content": text[:1000]}],
            },
        )
    except httpx.TransportError as exc:
        raise DescribeError(f"Could not reach the Anthropic API: {exc}") from exc
    finally:
        if http is None:
            client.close()
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text[:200])
        except ValueError:
            msg = resp.text[:200]
        raise DescribeError(f"Anthropic API error {resp.status_code}: {msg}")
    body = resp.json()
    out = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise DescribeError("Claude's reply didn't contain a recipe; try rephrasing.")
    try:
        data = json.loads(m.group(0))
    except ValueError as exc:
        raise DescribeError("Claude's reply wasn't valid JSON; try again.") from exc
    return _validate(text, data, engine=f"claude ({model})")


def _validate(text: str, data: dict[str, Any], *, engine: str) -> Recipe:
    recipe = Recipe(text=text, engine=engine)
    tags = data.get("tags") or {}
    if isinstance(tags, list):
        tags = {t: 1.0 for t in tags}
    for t, w in list(tags.items())[:20]:
        if not isinstance(t, str) or not t.strip():
            continue
        try:
            w = float(w)
        except (TypeError, ValueError):
            continue
        recipe.tags[t.strip().lower()[:40]] = round(min(1.0, max(0.1, w)), 2)
    recipe.avoid = [
        a.strip().lower()[:40] for a in (data.get("avoid") or []) if isinstance(a, str)
    ][:15]
    yrs = data.get("years")
    if (
        isinstance(yrs, list)
        and len(yrs) == 2
        and all(isinstance(y, int) and 1900 <= y <= 2100 for y in yrs)
        and yrs[0] <= yrs[1]
    ):
        recipe.years = yrs
    else:
        recipe.years = years_from_text(text)
    recipe.note = str(data.get("note") or "")[:200]
    if not recipe.tags:
        raise DescribeError("Claude returned no usable tags; try a more specific description.")
    return recipe


def translate(
    text: str,
    *,
    engine: str,
    api_key: str = "",
    model: str = DEFAULT_MODEL,
    http: httpx.Client | None = None,
    classes: vibes.TagClasses | None = None,
) -> Recipe:
    if engine == "offline" or (engine == "auto" and not api_key):
        recipe = translate_offline(text)
    else:
        recipe = translate_claude(text, api_key, model=model, http=http)
    # Judge songs by sound: genre / era words only if you typed them (metadata/vibes.py)
    cls = classes or vibes.TagClasses()
    recipe.tags = vibes.vibe_only(recipe.tags, text, cls)
    recipe.avoid = vibes.vibe_only_avoid(recipe.avoid, text, cls)
    recipe.tags = {t: w for t, w in recipe.tags.items() if t not in recipe.avoid}
    return recipe


# ---------------------------------------------------------------------------
# Grounding against your library (local only)
# ---------------------------------------------------------------------------


@dataclass
class TagReport:
    tag: str
    weight: float
    songs: int
    suggestions: list[tuple[str, int]] = field(default_factory=list)


def library_tag_counts(descriptors: dict[str, Descriptors], threshold: float = 0.3) -> Counter:
    counts: Counter = Counter()
    for d in descriptors.values():
        for t, w in d.sound.items():
            if w >= threshold:
                counts[t] += 1
    return counts


def ground(recipe: Recipe, descriptors: dict[str, Descriptors]) -> list[TagReport]:
    """How many of your songs carry each recipe tag, with close alternatives for misses."""
    counts = library_tag_counts(descriptors)
    vocab = [t for t, c in counts.items() if c >= 2]
    reports = []
    for tag, w in sorted(recipe.tags.items(), key=lambda kv: -kv[1]):
        n = counts.get(tag, 0)
        sugg: list[tuple[str, int]] = []
        if n == 0:
            close = difflib.get_close_matches(tag, vocab, n=3, cutoff=0.75)
            close += [v for v in vocab if tag in v.split() or v in tag.split()][:3]
            sugg = [(c, counts[c]) for c in dict.fromkeys(close) if c != tag][:3]
        reports.append(TagReport(tag, w, n, sugg))
    return reports


# ---------------------------------------------------------------------------
# Examples you name ("Nujabes", "Luv(sic)"), matched locally against your library
# ---------------------------------------------------------------------------


@dataclass
class ExampleMatch:
    query: str
    kind: str  # artist | song | none
    uris: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


def _split_artists(name: str) -> list[str]:
    """'Nujabes, Cise Starr' / 'A & B' / 'A feat. B' -> each name."""
    parts = re.split(r",|&| x | feat\.? | ft\.? | with ", name, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()] or [name]


def _compact(text: str) -> str:
    from playlist_curator.crate.deduplication import parse_title

    return parse_title(text).base.replace(" ", "")


def resolve_examples(
    db: Any, queries: list[str], *, max_per_artist: int = 40
) -> list[ExampleMatch]:
    """Find songs in *your* crate matching artist names or song titles. Nothing new is added.

    Artist names match exactly (ignoring case, accents, "The"). Song titles match exactly,
    then by prefix ("luvsic" finds "Luv(sic) Part 3"), then loosely (typos).
    """
    from playlist_curator.crate.deduplication import ensure_current, normalize_artist

    ensure_current(db)
    rows = db.conn.execute("SELECT primary_uri, title, artists_json FROM recordings").fetchall()
    recs = [
        (r["primary_uri"], r["title"], json.loads(r["artists_json"]), _compact(r["title"]))
        for r in rows
    ]
    out = []
    for q in queries:
        qa, qt = normalize_artist(q), _compact(q)
        by_artist = [
            (u, t, a)
            for u, t, a, _ in recs
            if any(normalize_artist(n) == qa for x in a for n in _split_artists(x))
        ]
        if by_artist:
            by_artist = sorted(by_artist, key=lambda x: x[1].lower())[:max_per_artist]
            out.append(
                ExampleMatch(
                    q,
                    "artist",
                    [u for u, _, _ in by_artist],
                    [f"{t} — {', '.join(a)}" for _, t, a in by_artist],
                )
            )
            continue
        exact = [(u, t, a) for u, t, a, c in recs if qt and c == qt]
        prefix = [(u, t, a) for u, t, a, c in recs if len(qt) >= 4 and c.startswith(qt)]
        fuzzy: list = []
        if not exact and not prefix and qt:
            names = {c: (u, t, a) for u, t, a, c in recs}
            fuzzy = [names[c] for c in difflib.get_close_matches(qt, list(names), n=3, cutoff=0.85)]
        hits = exact or prefix or fuzzy
        out.append(
            ExampleMatch(
                q,
                "song" if hits else "none",
                [u for u, _, _ in hits],
                [f"{t} — {', '.join(a)}" for _, t, a in hits],
            )
        )
    return out


def example_tags(
    uris: list[str], db: Any, descriptors: dict[str, Descriptors], *, min_support: float = 0.5
) -> list[tuple[str, float]]:
    """Tags shared by most of your example songs (MusicBrainz / Last.fm data, computed locally)."""
    rec_of = {
        r[0]: r[1] for r in db.conn.execute("SELECT uri, recording_id FROM recording_members")
    }
    vecs = [descriptors[rec_of[u]].sound for u in uris if rec_of.get(u) in descriptors]
    vecs = [v for v in vecs if v]
    if not vecs:
        return []
    counts: Counter = Counter()
    for v in vecs:
        for t, w in v.items():
            if w >= 0.3:
                counts[t] += 1
    return [
        (t, round(c / len(vecs), 2))
        for t, c in counts.most_common(12)
        if c / len(vecs) >= min_support
    ]


def apply_recipe(
    db: Any,
    recipe: Recipe,
    *,
    create: str | None = None,
    apply_to: str | None = None,
    example_uris: list[str] | None = None,
    kind: str = "mood",
) -> int:
    """Create a playlist from a recipe, or merge it into an existing one. Returns its id."""
    from playlist_curator.curator import identities as idn
    from playlist_curator.storage.database import utcnow

    if create:
        ident = idn.create_identity(db, create, kind=kind, concept=recipe.text)
    elif apply_to:
        ident = idn.get_identity(db, apply_to)
    else:
        raise ValueError("pass create or apply_to")
    idn.edit_identity(
        db,
        ident.identity_id,
        add_tags=recipe.tags,
        avoid_tags=recipe.avoid,
        years=recipe.years if recipe.years else "keep",
    )
    now = utcnow()
    db.conn.executemany(
        "INSERT OR REPLACE INTO identity_examples VALUES (?,?,?,?,?)",
        [(ident.identity_id, u, "reference", "user", now) for u in example_uris or []],
    )
    return ident.identity_id
