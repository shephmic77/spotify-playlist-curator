"""Judge every song as an individual, by how it sounds.

Community tags mix four kinds of words:

- **vibe**: what the song sounds or feels like: chill, mellow, funky, groovy, upbeat, dreamy,
  melancholic, energetic, instrumental, piano, female vocals, late night, summer ...
- **genre**: scene labels that are usually inherited from the *artist*: rap, hip hop, rock,
  pop, metal, country, edm ... "Lavender Buds" is MF DOOM, so taggers write "hip hop", even
  though it sounds chill and jazzy.
- **era**: 80s, 90s, oldies ...
- **ignore**: places, nationalities, years, "cover", "live" ...

By default a song is judged only on its **own** vibe: tags on the song itself (Last.fm track,
MusicBrainz recording) plus AcousticBrainz's audio-derived measures. Artist-level tags are not
used, except as a clearly flagged last resort for songs with no song-level data at all.
Genre and era words count **only in playlists that ask for them** (a playlist whose
definition mentions "80s" or "rap"). Some genres also say something about the sound, so they
add a softer vibe word ("jazz" → jazzy, "funk" → funky, "metal" → heavy, "lo-fi" → lo-fi).

You can reclassify any tag (``curator tags set "indie" vibe``); your choice always wins.
Everything here is a fixed, readable rule set: no model, nothing learned from your library.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from playlist_curator.storage.database import Database, utcnow

CLASSES = ("vibe", "genre", "era", "ignore")

# Weights
SOUND_GENRE_WEIGHT = 0.6  # a genre's implied vibe counts less than a direct vibe word
OTHER_WEIGHT = 0.5  # unclassified tags count as half a vibe word
ARTIST_FALLBACK = 0.5  # only for songs with no song-level data at all

# Vibe words, with spelling variants folded together
VIBE_ALIASES: dict[str, str] = {
    "chillout": "chill",
    "chill out": "chill",
    "chilled": "chill",
    "chill vibes": "chill",
    "relaxing": "relaxed",
    "relax": "relaxed",
    "calming": "calm",
    "soothing": "calm",
    "peaceful": "calm",
    "tranquil": "calm",
    "melancholy": "melancholic",
    "melancholia": "melancholic",
    "sadness": "sad",
    "depressing": "sad",
    "feel good": "feel-good",
    "feelgood": "feel-good",
    "good vibes": "feel-good",
    "uplifting": "feel-good",
    "happy": "happy",
    "upbeat": "upbeat",
    "energetic": "energetic",
    "high energy": "energetic",
    "energy": "energetic",
    "hype": "hype",
    "pump up": "hype",
    "party": "party",
    "dance": "danceable",
    "dancing": "danceable",
    "danceable": "danceable",
    "groove": "groovy",
    "groovy": "groovy",
    "funky": "funky",
    "jazzy": "jazzy",
    "soulful": "soulful",
    "smooth": "smooth",
    "mellow": "mellow",
    "laid back": "laid-back",
    "laidback": "laid-back",
    "easy listening": "easy listening",
    "dreamy": "dreamy",
    "ethereal": "dreamy",
    "hazy": "dreamy",
    "atmospheric": "atmospheric",
    "atmosphere": "atmospheric",
    "spacey": "atmospheric",
    "space": "atmospheric",
    "moody": "moody",
    "dark": "dark",
    "haunting": "dark",
    "eerie": "dark",
    "aggressive": "aggressive",
    "angry": "aggressive",
    "intense": "intense",
    "epic": "epic",
    "cinematic": "cinematic",
    "beautiful": "beautiful",
    "emotional": "emotional",
    "introspective": "introspective",
    "nostalgic": "nostalgic",
    "nostalgia": "nostalgic",
    "romantic": "romantic",
    "love songs": "romantic",
    "love song": "romantic",
    "sexy": "sensual",
    "sensual": "sensual",
    "catchy": "catchy",
    "sing along": "sing-along",
    "singalong": "sing-along",
    "anthem": "anthemic",
    "anthemic": "anthemic",
    "quirky": "quirky",
    "weird": "quirky",
    "experimental": "experimental",
    "psychedelic": "psychedelic",
    "trippy": "psychedelic",
    "lo-fi": "lo-fi",
    "lush": "lush",
    "warm": "warm",
    "raw": "raw",
    "heavy": "heavy",
    "loud": "loud",
    "soft": "soft",
    "quiet": "soft",
    "gentle": "soft",
    "slow": "slow",
    "fast": "fast",
    "instrumental": "instrumental",
    "acoustic": "acoustic",
    "piano": "piano",
    "guitar": "guitar",
    "guitar solo": "guitar",
    "acoustic guitar": "acoustic",
    "drums": "drums",
    "bass": "bass",
    "bassline": "bass",
    "synth": "synth",
    "synths": "synth",
    "strings": "strings",
    "orchestral": "orchestral",
    "saxophone": "saxophone",
    "sax": "saxophone",
    "horns": "horns",
    "brass": "horns",
    "female vocalists": "female vocals",
    "female vocalist": "female vocals",
    "female vocals": "female vocals",
    "male vocalists": "male vocals",
    "male vocals": "male vocals",
    "beats": "beats",
    "summer": "summer",
    "beach": "summer",
    "sunny": "summer",
    "sunshine": "summer",
    "night": "late night",
    "late night": "late night",
    "nocturnal": "late night",
    "midnight": "late night",
    "driving": "driving",
    "road trip": "driving",
    "study": "focus",
    "studying": "focus",
    "focus": "focus",
    "concentration": "focus",
    "workout": "workout",
    "gym": "workout",
    "running": "workout",
    "sleep": "sleep",
    "rainy day": "rainy day",
    "rain": "rainy day",
    "morning": "morning",
    "sunset": "sunset",
    "cozy": "cozy",
}
# every canonical vibe word is also a vibe word itself ("chill", "sad", "relaxed", ...)
for _v in set(VIBE_ALIASES.values()):
    VIBE_ALIASES.setdefault(_v, _v)

# Genres that also describe the sound: they keep their genre label (for playlists that ask
# for the genre) and add these vibe words at SOUND_GENRE_WEIGHT.
SOUND_OF: dict[str, dict[str, float]] = {
    "jazz": {"jazzy": 1.0},
    "jazz hop": {"jazzy": 1.0, "lo-fi": 0.8, "chill": 0.8},
    "jazzhop": {"jazzy": 1.0, "lo-fi": 0.8, "chill": 0.8},
    "chillhop": {"chill": 1.0, "lo-fi": 0.8, "jazzy": 0.6},
    "lo-fi hip hop": {"lo-fi": 1.0, "chill": 0.8},
    "acid jazz": {"jazzy": 1.0, "groovy": 0.8},
    "smooth jazz": {"jazzy": 1.0, "smooth": 1.0},
    "nu jazz": {"jazzy": 1.0},
    "funk": {"funky": 1.0, "groovy": 0.8},
    "p-funk": {"funky": 1.0},
    "funk rock": {"funky": 1.0},
    "soul": {"soulful": 1.0},
    "neo soul": {"soulful": 1.0, "smooth": 0.6},
    "neo-soul": {"soulful": 1.0, "smooth": 0.6},
    "motown": {"soulful": 1.0},
    "disco": {"groovy": 1.0, "danceable": 1.0},
    "nu disco": {"groovy": 1.0, "danceable": 1.0},
    "house": {"danceable": 1.0},
    "deep house": {"danceable": 0.8, "chill": 0.5},
    "ambient": {"atmospheric": 1.0, "calm": 0.6},
    "downtempo": {"chill": 1.0, "slow": 0.6},
    "trip hop": {"moody": 1.0, "chill": 0.6},
    "trip-hop": {"moody": 1.0, "chill": 0.6},
    "chillwave": {"chill": 1.0, "dreamy": 0.8},
    "dream pop": {"dreamy": 1.0},
    "shoegaze": {"dreamy": 1.0, "loud": 0.5},
    "psychedelic rock": {"psychedelic": 1.0},
    "neo-psychedelia": {"psychedelic": 1.0},
    "synthwave": {"synth": 1.0, "nostalgic": 0.6},
    "synthpop": {"synth": 1.0},
    "electropop": {"synth": 0.8},
    "new wave": {"synth": 0.6},
    "electronic": {"synth": 0.5},
    "electronica": {"synth": 0.5},
    "metal": {"heavy": 1.0},
    "heavy metal": {"heavy": 1.0},
    "nu metal": {"heavy": 1.0, "aggressive": 0.6},
    "metalcore": {"heavy": 1.0, "aggressive": 0.8},
    "hard rock": {"heavy": 0.8, "loud": 0.6},
    "grunge": {"heavy": 0.7, "raw": 0.6},
    "punk": {"raw": 0.8, "fast": 0.6},
    "punk rock": {"raw": 0.8, "fast": 0.6},
    "post-hardcore": {"heavy": 0.8, "intense": 0.6},
    "stoner rock": {"heavy": 0.8},
    "post-rock": {"atmospheric": 1.0, "epic": 0.6},
    "indie": {"indie": 1.0},
    "indie rock": {"indie": 1.0},
    "indie pop": {"indie": 1.0, "catchy": 0.5},
    "indie folk": {"indie": 1.0, "acoustic": 0.7},
    "bedroom pop": {"indie": 1.0, "lo-fi": 0.6},
    "folk": {"acoustic": 0.8},
    "singer-songwriter": {"acoustic": 0.6},
    "singer songwriter": {"acoustic": 0.6},
    "bossa nova": {"mellow": 1.0, "jazzy": 0.6},
    "reggae": {"laid-back": 1.0},
    "surf rock": {"summer": 0.8},
    "surf": {"summer": 0.8},
    "classical": {"orchestral": 0.8},
    "soundtrack": {"cinematic": 0.8},
    "score": {"cinematic": 0.8},
    "video game music": {"cinematic": 0.5},
    "emo": {"emotional": 0.8},
    "slowcore": {"slow": 1.0, "melancholic": 0.6},
    "sadcore": {"sad": 1.0},
    "blues": {"soulful": 0.6},
    "gospel": {"soulful": 0.8},
    "r&b": {"smooth": 0.6},
    "trap": {"bass": 0.6},
    "drum and bass": {"fast": 0.8, "danceable": 0.6},
    "dubstep": {"bass": 1.0, "heavy": 0.5},
    "edm": {"danceable": 0.8, "energetic": 0.6},
    "trance": {"danceable": 0.8, "energetic": 0.6},
    "techno": {"danceable": 0.8},
}

GENRE_EXACT = {
    "rock",
    "pop",
    "rap",
    "hip hop",
    "alternative",
    "alternative rock",
    "classic rock",
    "pop rock",
    "soft rock",
    "folk rock",
    "garage rock",
    "progressive rock",
    "prog rock",
    "art rock",
    "blues rock",
    "southern rock",
    "britpop",
    "post-punk",
    "pop punk",
    "hardcore",
    "screamo",
    "country",
    "americana",
    "bluegrass",
    "latin",
    "reggaeton",
    "k-pop",
    "j-pop",
    "city pop",
    "underground hip hop",
    "east coast hip hop",
    "west coast hip hop",
    "gangsta rap",
    "conscious hip hop",
    "alternative hip hop",
    "experimental hip hop",
    "abstract hip hop",
    "instrumental hip hop",
    "jazz rap",
    "boom bap",
    "dirty south",
    "grime",
    "drill",
    "uk hip hop",
    "cloud rap",
    "emo rap",
    "dance",
    "dance pop",
    "electro",
    "idm",
    "big beat",
    "breakbeat",
    "industrial",
    "musical",
    "christian",
    "world",
    "ska",
    "power pop",
    "art pop",
    "chamber pop",
    "baroque pop",
    "noise rock",
    "math rock",
    "midwest emo",
    "alt-country",
    "rockabilly",
    "doo wop",
    "swing",
    "big band",
    "bebop",
    "hard bop",
    "cool jazz",
    "fusion",
    "jazz fusion",
    "rnb",
    "contemporary r&b",
    "soul jazz",
    "anime",
    "opera",
    "baroque",
    "romantic era",
    "minimalism",
    "pop soul",
    "philly soul",
    "northern soul",
    "electro swing",
    "hip-hop",
}
GENRE_PATTERN = re.compile(
    r"\b(rock|hip hop|rap|metal|punk|pop|core|country|trap|drill|r&b|techno|trance|house|"
    r"step|edm|wave|jazz|soul|funk|folk|blues|disco|hop|gaze|grunge|emo)\b"
)
ERA_PATTERN = re.compile(r"^\d0s$")
ERA_WORDS = {"oldies", "golden oldies", "retro", "classic", "classics", "throwback", "vintage"}
IGNORE_EXACT = {
    "american",
    "british",
    "english",
    "uk",
    "usa",
    "us",
    "canadian",
    "australian",
    "irish",
    "scottish",
    "french",
    "german",
    "japanese",
    "korean",
    "swedish",
    "icelandic",
    "brazilian",
    "mexican",
    "spanish",
    "italian",
    "dutch",
    "norwegian",
    "danish",
    "finnish",
    "new zealand",
    "los angeles",
    "new york",
    "london",
    "california",
    "seattle",
    "chicago",
    "detroit",
    "atlanta",
    "texas",
    "cover",
    "covers",
    "live",
    "remix",
    "remixes",
    "single",
    "album",
    "albums",
    "ep",
    "hits",
    "top 40",
    "radio",
    "chart",
    "billboard",
    "grammy",
    "male",
    "female",
    "band",
    "duo",
    "producer",
    "rapper",
    "singer",
}
IGNORE_PATTERN = re.compile(r"^\d{4}$|^\d{4}s?$|\b(records|recordings|label)\b")


def default_class(tag: str) -> str:
    """Built-in class for a normalized tag (before your overrides)."""
    if tag in VIBE_ALIASES:
        return "vibe"
    if ERA_PATTERN.match(tag) or tag in ERA_WORDS:
        return "era"
    if tag in IGNORE_EXACT or IGNORE_PATTERN.search(tag):
        return "ignore"
    if tag in SOUND_OF or tag in GENRE_EXACT or GENRE_PATTERN.search(tag):
        return "genre"
    return "other"


@dataclass
class TagClasses:
    overrides: dict[str, str] = field(default_factory=dict)

    def of(self, tag: str) -> str:
        return self.overrides.get(tag) or default_class(tag)

    def is_label(self, tag: str) -> bool:
        return self.of(tag) in ("genre", "era")


def load_classes(db: Database | None) -> TagClasses:
    if db is None:
        return TagClasses()
    try:
        rows = db.conn.execute("SELECT tag, class FROM tag_classes").fetchall()
    except Exception:  # older schema in a test helper
        return TagClasses()
    return TagClasses({r[0]: r[1] for r in rows})


def set_class(db: Database, tag: str, cls: str | None) -> None:
    """Reclassify a tag (None = back to the built-in rule)."""
    tag = tag.strip().lower()
    if cls is None:
        db.conn.execute("DELETE FROM tag_classes WHERE tag=?", (tag,))
        return
    if cls not in CLASSES:
        raise ValueError(f"class must be one of {', '.join(CLASSES)}")
    db.conn.execute(
        "INSERT OR REPLACE INTO tag_classes (tag, class, updated_at) VALUES (?,?,?)",
        (tag, cls, utcnow()),
    )


SONG_SOURCES = ("lastfm_track", "mb_recording", "acousticbrainz")
ARTIST_SOURCES = ("lastfm_artist", "mb_artist")


@dataclass
class SongProfile:
    vibe: dict[str, float]  # always used
    labels: dict[str, float]  # genre / era: used only by playlists that ask for them
    evidence: str  # song | artist (fallback) | none

    @property
    def merged(self) -> dict[str, float]:
        out = dict(self.labels)
        for t, w in self.vibe.items():
            out[t] = max(out.get(t, 0.0), w)
        return out


def _fold(
    tags: dict[str, float],
    classes: TagClasses,
    vibe: dict[str, float],
    labels: dict[str, float],
    scale: float = 1.0,
) -> None:
    def put(d: dict[str, float], k: str, w: float) -> None:
        if w > 0:
            d[k] = max(d.get(k, 0.0), round(w * scale, 4))

    for tag, w in tags.items():
        cls = classes.of(tag)
        if cls == "ignore":
            continue
        if cls == "vibe":
            put(vibe, VIBE_ALIASES.get(tag, tag), w)
        elif cls == "other":
            put(vibe, tag, OTHER_WEIGHT * w)
        else:  # genre / era
            put(labels, tag, w)
            for adj, f in SOUND_OF.get(tag, {}).items():
                put(vibe, adj, SOUND_GENRE_WEIGHT * f * w)


def profile(by_source: dict[str, dict[str, float]], classes: TagClasses) -> SongProfile:
    """How one song sounds, from its own data first; artist tags only as a flagged fallback."""
    vibe: dict[str, float] = {}
    labels: dict[str, float] = {}
    for src in SONG_SOURCES:
        if by_source.get(src):
            _fold(by_source[src], classes, vibe, labels)
    if vibe or labels:
        return SongProfile(vibe, labels, "song")
    for src in ARTIST_SOURCES:
        if by_source.get(src):
            _fold(by_source[src], classes, vibe, labels, scale=ARTIST_FALLBACK)
    return SongProfile(vibe, labels, "artist" if (vibe or labels) else "none")


def requested_labels(rules: dict[str, Any], classes: TagClasses) -> set[str]:
    """Genre / era tags a playlist definition explicitly mentions (so they count there)."""
    mentioned = set((rules.get("tags") or {}).keys())
    mentioned |= set(rules.get("require_tags") or []) | set(rules.get("avoid_tags") or [])
    return {t for t in mentioned if classes.is_label(t)}


def _typed(tag: str, text: str) -> bool:
    low = f" {re.sub(r'[^a-z0-9&]+', ' ', text.lower())} "
    return f" {tag} " in low or any(f" {part} " in low for part in tag.split() if len(part) > 3)


def vibe_only_avoid(avoid: list[str], text: str, classes: TagClasses) -> list[str]:
    """Genre / era words to avoid survive only if you typed them; a sound genre you didn't
    type becomes its strongest vibe words ("no metal" from a 'study' preset -> avoid heavy)."""
    out: list[str] = []
    for tag in avoid:
        if not classes.is_label(tag) or _typed(tag, text):
            out.append(VIBE_ALIASES.get(tag, tag) if classes.of(tag) == "vibe" else tag)
            continue
        out += [adj for adj, f in SOUND_OF.get(tag, {}).items() if f >= 0.8]
    return list(dict.fromkeys(out))


def vibe_only(tags: dict[str, float], text: str, classes: TagClasses) -> dict[str, float]:
    """Filter a description recipe: genre / era tags survive only if you typed them (or a word
    of them); otherwise a sound genre turns into its vibe words and a pure genre is dropped."""
    out: dict[str, float] = {}
    for tag, w in tags.items():
        if not classes.is_label(tag):
            key = VIBE_ALIASES.get(tag, tag) if classes.of(tag) == "vibe" else tag
            out[key] = max(out.get(key, 0.0), w)
            continue
        if _typed(tag, text):
            out[tag] = max(out.get(tag, 0.0), w)
        for adj, f in SOUND_OF.get(tag, {}).items():
            out[adj] = max(out.get(adj, 0.0), round(SOUND_GENRE_WEIGHT * f * w, 2))
    return out
