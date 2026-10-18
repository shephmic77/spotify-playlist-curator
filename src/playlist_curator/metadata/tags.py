"""Normalization of community tags from MusicBrainz and Last.fm.

Tags are crowd-sourced and noisy. We keep descriptive ones (genre, era, mood, instrument,
vocal style, place) and drop ones that describe the *tagger* rather than the music
("seen live", "favorites", "albums I own"). Nothing is inferred: a tag either came from a
provider or it isn't there.
"""

from __future__ import annotations

import re

from playlist_curator.crate.deduplication import normalize_artist

_ALIASES = {
    "hiphop": "hip hop",
    "rnb": "r&b",
    "r and b": "r&b",
    "rhythm and blues": "r&b",
    "synth pop": "synthpop",
    "electropop": "electropop",
    "alternative": "alternative",
    "alt rock": "alternative rock",
    "indie": "indie",
    "post punk": "post-punk",
    "lofi": "lo-fi",
    "lo fi": "lo-fi",
    "drum n bass": "drum and bass",
    "dnb": "drum and bass",
    "edm": "edm",
    "chill": "chill",
    "chillout": "chillout",
    "chill out": "chillout",
}

_JUNK_EXACT = {
    "seen live",
    "favorites",
    "favourites",
    "favorite",
    "favourite",
    "my favorite",
    "my favourite",
    "favorite songs",
    "favourite songs",
    "love",
    "loved",
    "love it",
    "awesome",
    "amazing",
    "cool",
    "good",
    "great",
    "best",
    "spotify",
    "albums i own",
    "check out",
    "to listen",
    "music",
    "song",
    "songs",
    "all",
    "fun",
    "other",
    "misc",
    "favorite artists",
    "favourite artists",
    "owned",
    "under the radar",
}
_JUNK_PATTERNS = [
    re.compile(r"^under \d+ listeners$"),
    re.compile(r"^\d+ of \d+ stars?$"),
    re.compile(r"^\d(\.\d)? stars?$"),
    re.compile(r"\b(my|i love|i like|favou?rite)\b"),
    re.compile(r"^(spotify|youtube|soundcloud|pandora)\b"),
]
_DECADE = re.compile(r"^(?:19|20)?([0-9])0'?s$")
_WORDY_DECADES = {
    "fifties": "50s",
    "sixties": "60s",
    "seventies": "70s",
    "eighties": "80s",
    "nineties": "90s",
}


def normalize_tag(name: str, *, artist: str | None = None) -> str | None:
    """Return a canonical tag, or None if it should be discarded."""
    t = name.strip().lower()
    t = t.replace("’", "'")
    t = re.sub(r"[_]+", " ", t)
    t = re.sub(r"\bpost[\s-]+", "post-", t)  # keep "post-punk", "post-rock" hyphenated
    t = re.sub(r"(?<!post)-+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) < 2 or len(t) > 40:
        return None
    if t in _WORDY_DECADES:
        return _WORDY_DECADES[t]
    m = _DECADE.match(t)
    if m:  # "1980s", "80's", "80s" -> "80s";  "2000s" -> "00s"
        return f"{m.group(1)}0s"
    t = _ALIASES.get(t, t)
    if t in _JUNK_EXACT or any(p.search(t) for p in _JUNK_PATTERNS):
        return None
    if artist and t == normalize_artist(artist):
        return None
    return t


def weights_from_counts(
    counts: dict[str, float], *, artist: str | None = None, min_weight: float = 0.1, top: int = 25
) -> dict[str, float]:
    """Normalize raw tag counts to 0..1 relative to the strongest tag, after cleaning."""
    merged: dict[str, float] = {}
    for raw, count in counts.items():
        tag = normalize_tag(raw, artist=artist)
        if tag is None or count is None or count <= 0:
            continue
        merged[tag] = max(merged.get(tag, 0.0), float(count))
    if not merged:
        return {}
    peak = max(merged.values())
    out = {t: round(c / peak, 4) for t, c in merged.items() if c / peak >= min_weight}
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:top])
