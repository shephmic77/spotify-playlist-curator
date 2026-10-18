"""Recording-level deduplication of the music crate.

A *track* is one Spotify URI (or one local-file entry). A *recording* is the actual
performance; several URIs can be the same recording (a single and the album copy, a
compilation reissue). Playlists should contain each recording once, so later phases work on
recordings, not URIs.

Rules, in order of authority:

1. **Your decisions** (``dedup_decisions``) always win. ``same`` joins two tracks; ``different``
   keeps them apart even if they share an ISRC.
2. **Same ISRC ⇒ same recording.** An ISRC identifies a specific recording, so this merges
   automatically. It's the only automatic merge.
3. **Everything else is a suggestion** for you to review, never merged automatically:
   - ``title_match``: different ISRCs, but same normalized title, same primary artist, same
     version markers, similar length (typically a remaster or a regional re-release).
   - ``local_match``: a local file that looks like a catalog recording in your crate.

Different *versions* are never suggested as duplicates: a title qualifier such as "Live",
"Remix", "Acoustic", "Demo", "Instrumental", "Radio Edit", "Sped Up" or "Taylor's Version"
gives the track a different version signature. Edition labels ("Remastered 2011", "Mono",
"Deluxe") don't change the signature but are mentioned in the suggestion so you can decide.

The ``recordings``, ``recording_members`` and ``dedup_suggestions`` tables are derived and
rebuilt from scratch after each import or decision; decisions are never touched by a rebuild.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from playlist_curator.models import Track
from playlist_curator.storage.database import Database, utcnow

# ---------------------------------------------------------------------------
# Title / artist normalization
# ---------------------------------------------------------------------------

# Qualifier categories. Patterns are matched against a lowercase qualifier string, e.g. the
# text inside "(...)", "[...]" or after " - ". Order matters: first category that matches wins.
_FEAT = re.compile(r"^(feat\.?|ft\.?|featuring|with)\s")
_NOISE = re.compile(
    r"\b(official\s+(music\s+)?(video|audio|visuali[sz]er|lyric\s+video)|lyric(s)?(\s+video)?|"
    r"music\s+video|visuali[sz]er|audio|hd|hq|4k|1080p|720p|explicit|clean|uncensored)\b"
)
_EDITION = re.compile(
    r"\b(remaster(ed)?|re-?master(ed)?|mono|stereo|deluxe|anniversary|expanded|bonus\s+track|"
    r"from\s+.+|soundtrack|ost|original\s+motion\s+picture|single\s+version|album\s+version|"
    r"\d{4}\s+(mix|version)|original\s+mix)\b"
)
_VERSIONS: list[tuple[str, re.Pattern[str]]] = [
    ("live", re.compile(r"\b(live|unplugged|in\s+concert|session)\b")),
    ("remix", re.compile(r"\b(remix(ed)?|rmx|mix|bootleg|flip|vip)\b")),
    ("acoustic", re.compile(r"\b(acoustic|stripped|unplugged)\b")),
    ("demo", re.compile(r"\bdemo\b")),
    ("instrumental", re.compile(r"\b(instrumental|karaoke|backing\s+track)\b")),
    ("a cappella", re.compile(r"\b(a\s*cap+el+a|vocals?\s+only)\b")),
    ("edit", re.compile(r"\b(edit|radio\s+version|short\s+version)\b")),
    ("extended", re.compile(r"\b(extended|long\s+version|12\s*(\"|inch))\b")),
    ("speed", re.compile(r"\b(sped\s+up|slowed|reverb|nightcore|daycore|8d)\b")),
    ("re-recorded", re.compile(r"\b(re-?recorded|taylor'?s\s+version|rerecord)")),
    ("cover", re.compile(r"\bcover\b")),
    ("orchestral", re.compile(r"\b(orchestral|symphonic|piano\s+version)\b")),
]

_EDITION_ONLY = re.compile(
    r"\b((original|\d{4}|stereo|mono)\s+(mix|version)|(single|album|\d{4})\s+version|"
    r"re-?master(ed)?(\s+version)?|\d{4})\b"
)
_BRACKETS = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")
_DASH_SUFFIX = re.compile(r"\s+[-–—]\s+(.+)$")
_FEAT_TAIL = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.+$", re.IGNORECASE)


def _fold(text: str) -> str:
    """Lowercase, strip accents, unify '&', drop punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ").replace("’", "'")
    text = re.sub(r"[^\w\s']", " ", text)
    text = text.replace("'", "")
    return re.sub(r"\s+", " ", text).strip()


def _classify(qualifier: str) -> tuple[str, str | None]:
    """Return (category, version_label) for one qualifier string."""
    q = qualifier.lower().strip()
    if not q:
        return "noise", None
    if _FEAT.match(q):
        return "feat", None
    # Phrases like "2011 Mix", "Original Mix", "Single Version", "Remastered" are edition
    # labels; strip them before looking for version words so "mix"/"version" don't misfire.
    stripped = _EDITION_ONLY.sub(" ", q)
    for label, pattern in _VERSIONS:
        if pattern.search(stripped):
            return "version", label
    if _EDITION.search(q):
        return "edition", None
    if _NOISE.search(q):
        return "noise", None
    return "unknown", None


@dataclass(frozen=True)
class TitleParts:
    base: str  # normalized title used for matching
    versions: frozenset[str]  # version signature, e.g. {"live"}
    editions: tuple[str, ...]  # raw edition qualifiers, for explanations only


def parse_title(title: str) -> TitleParts:
    """Split a title into a matchable base plus version/edition qualifiers.

    Qualifiers are only taken from brackets and a trailing " - ..." segment, never from the
    base title itself, so "Live and Let Die" stays one title and doesn't count as a live
    version. Bracketed text that isn't a recognised qualifier stays part of the title, e.g.
    "(Don't Fear) The Reaper".
    """
    versions: set[str] = set()
    editions: list[str] = []
    work = title.strip()

    # Trailing " - Remastered 2011" / " - Live at Wembley" (may repeat)
    while True:
        m = _DASH_SUFFIX.search(work)
        if not m:
            break
        category, label = _classify(m.group(1))
        if category == "unknown":
            break
        if category == "version" and label:
            versions.add(label)
        elif category == "edition":
            editions.append(m.group(1).strip())
        work = work[: m.start()]

    kept: list[str] = []
    last = 0
    for m in _BRACKETS.finditer(work):
        category, label = _classify(m.group(1))
        if category == "unknown":
            continue  # leave it in the base title
        kept.append(work[last : m.start()])
        last = m.end()
        if category == "version" and label:
            versions.add(label)
        elif category == "edition":
            editions.append(m.group(1).strip())
    kept.append(work[last:])
    base = _FEAT_TAIL.sub("", "".join(kept))
    return TitleParts(_fold(base), frozenset(versions), tuple(editions))


def normalize_artist(name: str) -> str:
    n = _fold(name)
    n = re.sub(r"\s+(topic|vevo|official)$", "", n)
    n = re.sub(r"^the\s+", "", n)
    return n


def local_title_and_artist(track: Track) -> tuple[str, str | None]:
    """Best-effort title/artist for a local file.

    Files ripped from YouTube often have no artist tag and a title like
    "Artist - Song (Official Video)". Only when the artist is missing do we split on " - ".
    """
    title = track.name or ""
    artist = track.artists[0].name if track.artists else None
    if not artist and re.search(r"\s[-–—]\s", title):
        left, right = re.split(r"\s[-–—]\s", title, maxsplit=1)
        # Don't split "Song - Remastered 2011": only split when the right side (ignoring
        # bracketed noise like "(Official Video)") isn't itself a qualifier.
        right_core = _BRACKETS.sub("", right).strip()
        if left.strip() and right_core and _classify(right_core)[0] == "unknown":
            artist, title = left.strip(), right.strip()
    return title, artist


def pair_key(uri_a: str, uri_b: str) -> str:
    a, b = sorted((uri_a, uri_b))
    return f"{a}|{b}"


def short_id(key: str) -> str:
    """Stable 8-char id for showing a suggestion on the command line."""
    return hashlib.sha1(key.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass
class Recording:
    recording_id: str
    primary_uri: str
    members: dict[str, str]  # uri -> evidence (self | isrc | accepted)
    title: str
    artists: list[str]

    @property
    def has_local(self) -> bool:
        return any(u.startswith("spotify:local:") for u in self.members)

    @property
    def has_catalog(self) -> bool:
        return any(not u.startswith("spotify:local:") for u in self.members)


@dataclass
class Suggestion:
    uri_a: str
    uri_b: str
    kind: str  # title_match | local_match
    strength: str  # strong | possible
    reason: str

    @property
    def key(self) -> str:
        return pair_key(self.uri_a, self.uri_b)


@dataclass
class DedupResult:
    recordings: list[Recording] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)

    @property
    def merged_groups(self) -> int:
        return sum(1 for r in self.recordings if len(r.members) > 1)


_ALBUM_RANK = {"album": 0, "single": 1, "compilation": 2}


def _primary(members: list[Track], source_counts: dict[str, int]) -> Track:
    """Pick the URI that will represent the recording when writing playlists.

    Prefer catalog over local, then the copy you use most, then an album release over a
    single over a compilation, then a playable copy, then the earliest release.
    """
    return min(
        members,
        key=lambda t: (
            t.is_local,
            -source_counts.get(t.uri, 0),
            _ALBUM_RANK.get(t.album_type or "", 3),
            t.is_playable is False,
            t.release_date or "9999",
            t.uri,
        ),
    )


def group_recordings(
    tracks: list[Track],
    decisions: dict[str, str],
    source_counts: dict[str, int] | None = None,
) -> list[Recording]:
    source_counts = source_counts or {}
    by_uri = {t.uri: t for t in tracks}
    uf = _UnionFind(by_uri)
    evidence: dict[str, str] = {}

    by_isrc: dict[str, list[str]] = defaultdict(list)
    for t in tracks:
        if t.isrc and not t.is_local:
            by_isrc[t.isrc.upper()].append(t.uri)
    for uris in by_isrc.values():
        for i, a in enumerate(uris):
            for b in uris[i + 1 :]:
                if decisions.get(pair_key(a, b)) == "different":
                    continue
                uf.union(a, b)
                evidence.setdefault(a, "isrc")
                evidence.setdefault(b, "isrc")

    for key, decision in decisions.items():
        if decision != "same":
            continue
        a, b = key.split("|", 1)
        if a in by_uri and b in by_uri:
            uf.union(a, b)
            evidence[a] = evidence[b] = "accepted"

    groups: dict[str, list[Track]] = defaultdict(list)
    for uri, t in by_uri.items():
        groups[uf.find(uri)].append(t)

    recordings = []
    used_ids: set[str] = set()
    # Deterministic order so ids are stable between rebuilds.
    for members in sorted(groups.values(), key=lambda ms: min(m.uri for m in ms)):
        primary = _primary(members, source_counts)
        isrcs = sorted({m.isrc.upper() for m in members if m.isrc and not m.is_local})
        rec_id = f"isrc:{isrcs[0]}" if isrcs else ""
        if not rec_id or rec_id in used_ids:
            # No ISRC, or you split copies sharing an ISRC apart: fall back to a URI id.
            rec_id = f"uri:{min(m.uri for m in members)}"
        used_ids.add(rec_id)
        recordings.append(
            Recording(
                recording_id=rec_id,
                primary_uri=primary.uri,
                members={
                    m.uri: evidence.get(m.uri, "self") if len(members) > 1 else "self"
                    for m in members
                },
                title=primary.name,
                artists=[a.name for a in primary.artists],
            )
        )
    recordings.sort(key=lambda r: r.recording_id)
    return recordings


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------


def _duration_gap(a: Track, b: Track) -> float | None:
    if not a.duration_ms or not b.duration_ms:
        return None
    return abs(a.duration_ms - b.duration_ms) / 1000


def _artist_compatible(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a == b or (len(a) > 3 and len(b) > 3 and (a in b or b in a))


def find_suggestions(
    recordings: list[Recording], tracks: list[Track], decisions: dict[str, str]
) -> list[Suggestion]:
    by_uri = {t.uri: t for t in tracks}
    rec_of = {uri: r.recording_id for r in recordings for uri in r.members}
    out: list[Suggestion] = []

    # Catalog vs catalog: compare recording representatives.
    buckets: dict[tuple[str, str, frozenset[str]], list[Track]] = defaultdict(list)
    for r in recordings:
        rep = by_uri[r.primary_uri]
        if rep.is_local or not rep.artists:
            continue
        parts = parse_title(rep.name)
        if not parts.base:
            continue
        buckets[(parts.base, normalize_artist(rep.artists[0].name), parts.versions)].append(rep)

    for reps in buckets.values():
        for i, a in enumerate(reps):
            for b in reps[i + 1 :]:
                if pair_key(a.uri, b.uri) in decisions:
                    continue
                gap = _duration_gap(a, b)
                if gap is not None and gap > 10:
                    continue
                strength = "strong" if gap is not None and gap <= 3 else "possible"
                editions = set(parse_title(a.name).editions) ^ set(parse_title(b.name).editions)
                reason = "Same title and artist, different ISRC"
                reason += f"; length differs by {gap:.0f}s" if gap is not None else ""
                if editions:
                    reason += f"; edition labels differ ({', '.join(sorted(editions))})"
                out.append(Suggestion(a.uri, b.uri, "title_match", strength, reason))

    # Local file vs catalog recordings.
    catalog_index: dict[str, list[Track]] = defaultdict(list)
    for r in recordings:
        rep = by_uri[r.primary_uri]
        if not rep.is_local:
            catalog_index[parse_title(rep.name).base].append(rep)

    for t in tracks:
        if not t.is_local:
            continue
        title, artist = local_title_and_artist(t)
        parts = parse_title(title)
        if not parts.base:
            continue
        local_artist = normalize_artist(artist) if artist else None
        for cand in catalog_index.get(parts.base, []):
            if rec_of.get(t.uri) == rec_of.get(cand.uri):
                continue  # already accepted
            if pair_key(t.uri, cand.uri) in decisions:
                continue
            if parse_title(cand.name).versions != parts.versions:
                continue
            cand_artist = normalize_artist(cand.artists[0].name) if cand.artists else None
            artist_ok = _artist_compatible(local_artist, cand_artist)
            if local_artist and not artist_ok:
                continue
            gap = _duration_gap(t, cand)
            if gap is not None and gap > 20:
                continue
            strong = artist_ok and gap is not None and gap <= 5
            bits = ["same title"]
            bits.append("same artist" if artist_ok else "local file has no artist tag")
            if gap is not None:
                bits.append(f"length differs by {gap:.0f}s")
            out.append(
                Suggestion(
                    t.uri,
                    cand.uri,
                    "local_match",
                    "strong" if strong else "possible",
                    "Local file looks like a song in your crate: " + ", ".join(bits),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_decisions(db: Database) -> dict[str, str]:
    return {
        r["pair_key"]: r["decision"]
        for r in db.conn.execute("SELECT pair_key, decision FROM dedup_decisions")
    }


def _source_counts(db: Database) -> dict[str, int]:
    return {
        r[0]: r[1]
        for r in db.conn.execute(
            "SELECT track_uri, COUNT(*) FROM source_entries GROUP BY track_uri"
        )
    }


def rebuild(db: Database) -> DedupResult:
    """Recompute recordings and suggestions from tracks + decisions (idempotent)."""
    tracks = list(db.iter_tracks())
    decisions = load_decisions(db)
    recordings = group_recordings(tracks, decisions, _source_counts(db))
    suggestions = find_suggestions(recordings, tracks, decisions)

    with db.transaction():
        db.conn.execute("DELETE FROM recording_members")
        db.conn.execute("DELETE FROM recordings")
        db.conn.execute("DELETE FROM dedup_suggestions")
        db.conn.executemany(
            "INSERT INTO recordings VALUES (?,?,?,?,?,?,?)",
            [
                (
                    r.recording_id,
                    r.primary_uri,
                    r.title,
                    json.dumps(r.artists),
                    len(r.members),
                    int(r.has_catalog),
                    int(r.has_local),
                )
                for r in recordings
            ],
        )
        db.conn.executemany(
            "INSERT INTO recording_members VALUES (?,?,?)",
            [(uri, r.recording_id, ev) for r in recordings for uri, ev in r.members.items()],
        )
        db.conn.executemany(
            "INSERT OR IGNORE INTO dedup_suggestions VALUES (?,?,?,?,?,?)",
            [(s.key, s.uri_a, s.uri_b, s.kind, s.strength, s.reason) for s in suggestions],
        )
    return DedupResult(recordings, suggestions)


def ensure_current(db: Database) -> None:
    """Rebuild if the derived tables don't cover exactly the current tracks."""
    tracks = db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    members = db.conn.execute(
        "SELECT COUNT(*) FROM recording_members rm JOIN tracks t ON t.uri = rm.uri"
    ).fetchone()[0]
    total_members = db.conn.execute("SELECT COUNT(*) FROM recording_members").fetchone()[0]
    if tracks != members or members != total_members:
        rebuild(db)


def decide(db: Database, uri_a: str, uri_b: str, decision: str) -> DedupResult:
    """Record that two tracks are the ``same`` recording or ``different`` ones, then rebuild."""
    if decision not in ("same", "different"):
        raise ValueError("decision must be 'same' or 'different'")
    if uri_a == uri_b:
        raise ValueError("a track can't be compared with itself")
    a, b = sorted((uri_a, uri_b))
    db.conn.execute(
        "INSERT OR REPLACE INTO dedup_decisions VALUES (?,?,?,?,?)",
        (pair_key(a, b), a, b, decision, utcnow()),
    )
    return rebuild(db)


def split_out(db: Database, uri: str) -> DedupResult:
    """Separate one track from every other member of its recording."""
    row = db.conn.execute(
        "SELECT recording_id FROM recording_members WHERE uri=?", (uri,)
    ).fetchone()
    if row is None:
        raise KeyError(uri)
    others = [
        r[0]
        for r in db.conn.execute(
            "SELECT uri FROM recording_members WHERE recording_id=? AND uri<>?",
            (row[0], uri),
        )
    ]
    now = utcnow()
    for other in others:
        a, b = sorted((uri, other))
        db.conn.execute(
            "INSERT OR REPLACE INTO dedup_decisions VALUES (?,?,?,?,?)",
            (pair_key(a, b), a, b, "different", now),
        )
    return rebuild(db)


def undo_decision(db: Database, key: str) -> DedupResult:
    db.conn.execute("DELETE FROM dedup_decisions WHERE pair_key=?", (key,))
    return rebuild(db)


def resolve_suggestion(db: Database, short: str) -> tuple[str, str]:
    """Find a pending suggestion by the short id shown on the command line."""
    matches = [
        (r["uri_a"], r["uri_b"])
        for r in db.conn.execute("SELECT pair_key, uri_a, uri_b FROM dedup_suggestions")
        if short_id(r["pair_key"]).startswith(short.lower())
    ]
    if not matches:
        raise KeyError(f"No pending suggestion with id {short!r}")
    if len(matches) > 1:
        raise KeyError(f"Id {short!r} is ambiguous; use more characters")
    return matches[0]
