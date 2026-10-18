"""Analysis of your existing playlists, treated as imperfect evidence of your preferences.

For each playlist that Spotify let us read, this reports:
- size, how much of it you also liked, when songs were first and last added
- era: original release years (MusicBrainz first-release year when known)
- artist concentration
- its tag profile: tags that are common in it *and* more common than in your crate overall
- cohesion: how similar its songs are to the playlist's own tag profile
- outliers: songs that look least like the rest (possible mismatches or drift)
- drift: how much the songs added later differ from the songs added first
And across playlists: overlap of songs and similarity of tag profiles (possible redundancy).

Every number is a transparent heuristic over independent tags (MusicBrainz / Last.fm),
release years and your own membership data. None of them is a probability, and thresholds
for the plain-language flags are documented constants, not calibrated values.
Songs without tags are left out of tag-based measures and reported as "untagged".
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from playlist_curator.crate.deduplication import ensure_current
from playlist_curator.metadata.descriptors import Descriptors, load_descriptors
from playlist_curator.storage.database import Database

# Flag thresholds (heuristics; see docs/scoring_methodology.md)
MIN_TAGGED_FOR_PROFILE = 5
LOW_COHESION = 0.30
DRIFT_SIMILARITY = 0.60
DRIFT_MIN_SONGS = 20
STALE_DAYS = 365
SMALL_PLAYLIST = 8
OUTLIER_MAX = 10
OVERLAP_CONTAINMENT = 0.6
OVERLAP_JACCARD = 0.4
PROFILE_SIMILAR = 0.90
TAG_SUPPORT_THRESHOLD = 0.3  # a tag "applies" to a song at or above this weight


Vector = dict[str, float]


def cosine(a: Vector, b: Vector) -> float:
    if not a or not b:
        return 0.0
    dot = sum(w * b.get(t, 0.0) for t, w in a.items())
    na = math.sqrt(sum(w * w for w in a.values()))
    nb = math.sqrt(sum(w * w for w in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def centroid(vectors: list[Vector]) -> Vector:
    if not vectors:
        return {}
    total: dict[str, float] = defaultdict(float)
    for v in vectors:
        for t, w in v.items():
            total[t] += w
    return {t: w / len(vectors) for t, w in total.items()}


@dataclass
class SongRef:
    recording_id: str
    title: str
    artists: str
    similarity: float | None = None


@dataclass
class PlaylistReport:
    playlist_id: str
    name: str
    owned: bool
    songs: int
    liked_share: float
    first_added: str | None
    last_added: str | None
    days_since_last_add: int | None
    tagged: int
    year_median: int | None
    year_p10: int | None
    year_p90: int | None
    decades: list[tuple[str, float]]
    top_artists: list[tuple[str, float]]
    unique_artists: int
    top_tags: list[dict[str, Any]]
    cohesion: float | None
    outliers: list[SongRef]
    drift_similarity: float | None
    flags: list[str] = field(default_factory=list)


@dataclass
class PairReport:
    a: str
    b: str
    a_id: str
    b_id: str
    shared_songs: int
    jaccard: float
    containment: float
    profile_similarity: float | None
    flag: str


@dataclass
class CollectionReport:
    generated_at: str
    playlists: list[PlaylistReport]
    pairs: list[PairReport]
    crate_recordings: int
    crate_tagged: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
    return values[idx]


def _year(release_date: str | None) -> int | None:
    if release_date and release_date[:4].isdigit() and int(release_date[:4]) > 0:
        return int(release_date[:4])
    return None


def analyze(db: Database, *, descriptors: dict[str, Descriptors] | None = None) -> CollectionReport:
    ensure_current(db)
    desc = descriptors if descriptors is not None else load_descriptors(db)
    vectors = {rid: d.combined for rid, d in desc.items() if d.by_source}

    # Recording info
    info: dict[str, dict[str, Any]] = {}
    for r in db.conn.execute(
        """SELECT r.recording_id, r.title, r.artists_json, t.release_date
           FROM recordings r JOIN tracks t ON t.uri = r.primary_uri"""
    ):
        artists = json.loads(r["artists_json"])
        year = _year(r["release_date"])
        mb_year = desc.get(r["recording_id"], Descriptors(r["recording_id"])).facts.get(
            "first_release_year"
        )
        if mb_year and (year is None or mb_year < year):
            year = mb_year
        info[r["recording_id"]] = {
            "title": r["title"],
            "artists": ", ".join(artists),
            "primary_artist": artists[0] if artists else "",
            "year": year,
        }

    liked = {
        r[0]
        for r in db.conn.execute(
            """SELECT DISTINCT rm.recording_id FROM source_entries se
               JOIN recording_members rm ON rm.uri = se.track_uri WHERE se.kind='liked'"""
        )
    }

    members: dict[str, dict[str, str | None]] = defaultdict(dict)  # pid -> rid -> added_at
    for r in db.conn.execute(
        """SELECT se.source_id, rm.recording_id, MIN(se.added_at) AS added_at
           FROM source_entries se JOIN recording_members rm ON rm.uri = se.track_uri
           WHERE se.kind = 'playlist' GROUP BY se.source_id, rm.recording_id"""
    ):
        members[r["source_id"]][r["recording_id"]] = r["added_at"]

    # Crate-wide tag baseline for lift
    crate_mean: dict[str, float] = defaultdict(float)
    for v in vectors.values():
        for t, w in v.items():
            crate_mean[t] += w
    n_tagged = max(len(vectors), 1)
    crate_mean = {t: w / n_tagged for t, w in crate_mean.items()}

    now = datetime.now(UTC)
    reports: list[PlaylistReport] = []
    profiles: dict[str, Vector] = {}
    names: dict[str, str] = {}
    for p in db.conn.execute(
        "SELECT * FROM playlists WHERE contents_status='fetched' ORDER BY name COLLATE NOCASE"
    ):
        pid = p["spotify_id"]
        names[pid] = p["name"]
        rids = list(members.get(pid, {}))
        if not rids:
            continue
        added = sorted(a for a in members[pid].values() if a)
        years = [info[r]["year"] for r in rids if r in info and info[r]["year"]]
        decades = Counter(f"{(y // 10) * 10 % 100:02d}s" for y in years)
        artist_counts = Counter(info[r]["primary_artist"] for r in rids if r in info)
        tagged = [r for r in rids if r in vectors]
        rep = PlaylistReport(
            playlist_id=pid,
            name=p["name"],
            owned=bool(p["owned_by_user"]),
            songs=len(rids),
            liked_share=round(sum(1 for r in rids if r in liked) / len(rids), 3),
            first_added=added[0][:10] if added else None,
            last_added=added[-1][:10] if added else None,
            days_since_last_add=(now - datetime.fromisoformat(added[-1])).days if added else None,
            tagged=len(tagged),
            year_median=int(statistics.median(years)) if years else None,
            year_p10=_percentile(years, 0.1),
            year_p90=_percentile(years, 0.9),
            decades=[(d, round(c / len(years), 3)) for d, c in decades.most_common(3)],
            top_artists=[
                (a, round(c / len(rids), 3)) for a, c in artist_counts.most_common(3) if a
            ],
            unique_artists=len(artist_counts),
            top_tags=[],
            cohesion=None,
            outliers=[],
            drift_similarity=None,
        )

        if len(tagged) >= MIN_TAGGED_FOR_PROFILE:
            prof = centroid([vectors[r] for r in tagged])
            profiles[pid] = prof
            tag_rows = []
            for t, mean_w in prof.items():
                support = sum(
                    1 for r in tagged if vectors[r].get(t, 0) >= TAG_SUPPORT_THRESHOLD
                ) / len(tagged)
                if support < 0.25:
                    continue
                lift = mean_w / crate_mean[t] if crate_mean.get(t) else 1.0
                tag_rows.append(
                    {
                        "tag": t,
                        "support": round(support, 3),
                        "lift": round(lift, 2),
                        "score": support * math.log2(1 + lift),
                    }
                )
            tag_rows.sort(key=lambda x: -x["score"])
            rep.top_tags = [{k: v for k, v in x.items() if k != "score"} for x in tag_rows[:8]]

            sims = {r: cosine(vectors[r], prof) for r in tagged}
            rep.cohesion = round(statistics.mean(sims.values()), 3)
            spread = statistics.pstdev(sims.values()) if len(sims) > 1 else 0.0
            # Far below the playlist's own spread AND well below its typical similarity, so a
            # 0.97 song in a 0.99 playlist isn't called a misfit.
            cutoff = max(0.15, min(rep.cohesion - 2 * spread, 0.5 * rep.cohesion))
            low = sorted((r for r in tagged if sims[r] < cutoff), key=lambda r: sims[r])
            rep.outliers = [
                SongRef(r, info[r]["title"], info[r]["artists"], round(sims[r], 3))
                for r in low[:OUTLIER_MAX]
            ]

            dated = sorted((members[pid][r], r) for r in tagged if members[pid].get(r))
            if len(dated) >= DRIFT_MIN_SONGS:
                half = len(dated) // 2
                early = centroid([vectors[r] for _, r in dated[:half]])
                late = centroid([vectors[r] for _, r in dated[half:]])
                rep.drift_similarity = round(cosine(early, late), 3)

        flags = rep.flags
        if rep.songs < SMALL_PLAYLIST:
            flags.append(f"small ({rep.songs} songs)")
        if rep.days_since_last_add is not None and rep.days_since_last_add > STALE_DAYS:
            flags.append(f"no songs added in {rep.days_since_last_add // 30} months")
        if rep.cohesion is not None and rep.cohesion < LOW_COHESION:
            flags.append("broad or mixed: songs don't share much in their tags")
        if rep.drift_similarity is not None and rep.drift_similarity < DRIFT_SIMILARITY:
            flags.append("drifted: later additions sound different from the early ones")
        if rep.tagged < MIN_TAGGED_FOR_PROFILE:
            flags.append("too few tagged songs to judge its sound")
        if rep.outliers:
            flags.append(f"{len(rep.outliers)} possible misfit(s)")
        reports.append(rep)

    # Pairwise overlap
    pairs: list[PairReport] = []
    pids = [r.playlist_id for r in reports]
    sets = {pid: set(members[pid]) for pid in pids}
    for i, a in enumerate(pids):
        for b in pids[i + 1 :]:
            shared = len(sets[a] & sets[b])
            psim = (
                round(cosine(profiles[a], profiles[b]), 3)
                if a in profiles and b in profiles
                else None
            )
            if shared == 0 and (psim is None or psim < PROFILE_SIMILAR):
                continue
            jac = shared / len(sets[a] | sets[b])
            cont = shared / min(len(sets[a]), len(sets[b]))
            if cont >= OVERLAP_CONTAINMENT:
                flag = "one is largely inside the other"
            elif jac >= OVERLAP_JACCARD:
                flag = "heavy song overlap"
            elif psim is not None and psim >= PROFILE_SIMILAR:
                flag = "similar sound, different songs"
            else:
                continue
            pairs.append(
                PairReport(
                    names[a], names[b], a, b, shared, round(jac, 3), round(cont, 3), psim, flag
                )
            )
    pairs.sort(key=lambda x: (-x.containment, -x.jaccard))

    # Playlist names aren't unique (two playlists can both be called "H"), so link by id.
    by_id = {r.playlist_id: r for r in reports}
    for pr in pairs:
        by_id[pr.a_id].flags.append(f"overlaps with '{pr.b}' ({pr.flag})")
        by_id[pr.b_id].flags.append(f"overlaps with '{pr.a}' ({pr.flag})")

    return CollectionReport(
        generated_at=now.isoformat(timespec="seconds"),
        playlists=reports,
        pairs=pairs,
        crate_recordings=len(info),
        crate_tagged=len(vectors),
    )
