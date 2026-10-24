"""Phase 4: score every song against every playlist and build draft playlists.

Multi-label and playlist-specific: each song is scored independently against each playlist's
own definition (identity), so a song can land in several playlists or in none.

For one song *s* and one playlist *p*:

1. **Hard rules** first.
   - Essential songs are always in; excluded songs are never in.
   - Required tags: *s* must carry one of them (weight ≥ 0.3), else it's out.
   - Avoided tags: a strong avoided tag (≥ 0.5) vetoes; a weaker one scales the score down.
2. **Tag score** = Σ wₜ·sₜ / Σ wₜ over the playlist's tags (song tag weight sₜ, 0 if absent).
   How much of what the playlist is "about" this song has.
3. **Example score** = mean cosine similarity to the 3 most similar example songs
   (excluding the song itself). "Sounds like the songs you said belong here."
4. **Combined** = 0.5·tag + 0.5·example (or whichever exists).
5. **Era**: if the playlist prefers years and the song's original year is known and outside
   them, the score is multiplied by 0.75. Unknown years aren't penalised.
6. **Threshold** adapts per playlist: the score a typical example song gets (30th
   percentile, leave-one-out), clamped to 0.30–0.75. A new song has to look at least as
   much like the playlist as most of its own examples do. Songs within 0.08 below the
   threshold go to *review*.

Example songs get no free pass: they're scored like any other song (minus themselves), so a
misfit example from an old playlist can drop out. Hand-picked playlists aren't scored; they
hold exactly their example songs.

Songs are judged as individuals (``metadata/vibes.py``): only the song's own tags and audio
measures count, as vibe words. Genre and era words count only in playlists whose definition
mentions them; artist-level tags are a flagged last resort for songs with no song data.

**Every song lands somewhere** (coverage): a song that clears no playlist's bar goes into the
playlist it comes closest to, relative to that playlist's bar, marked as a weak fit. It never
goes into a playlist where you rejected it, and hand-picked playlists never get extras.

Scores are heuristics for ranking and explanation, **not probabilities**. ``evidence`` says
where the song's data came from (song-level, artist-level fallback, or none).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from playlist_curator.crate.deduplication import ensure_current
from playlist_curator.curator.identities import Identity, list_identities
from playlist_curator.metadata import vibes
from playlist_curator.metadata.descriptors import Descriptors, load_descriptors
from playlist_curator.storage.database import Database, utcnow

TAG_PRESENT = 0.3
AVOID_VETO = 0.5
ERA_PENALTY = 0.75
TOP_K_EXAMPLES = 3
THRESHOLD_PERCENTILE = 0.30
THRESHOLD_MIN, THRESHOLD_MAX = 0.30, 0.75
REVIEW_BAND = 0.08
DEFAULT_THRESHOLD = 0.45  # when a playlist has fewer than 3 scorable examples
REJECT_SIMILAR = 0.85  # closer than this to a rejected song (and closer than to any example)
REJECT_PENALTY = 0.80
MAX_SHARE = 0.20  # a playlist may hold at most this share of the tagged library
MAX_OVERLAPS_REPORTED = 10
OVERLAP_REPORT = 0.7
AUTO_EXAMPLES = 5  # a playlist with fewer examples from you is topped up to this many
AUTO_MIN_TAG_SCORE = 0.2  # an automatic example must match the description at least this well
STICKY_RANK = (
    0.05  # a weak-fit song stays in last draft's playlist unless another is clearly closer
)
WEAK_FIT = "closest match (weak fit)"


@dataclass
class SongScore:
    recording_id: str
    decision: str  # include | review | out
    score: float
    tag_score: float | None
    example_score: float | None
    evidence: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class PlaylistDraft:
    identity: Identity
    threshold: float | None
    include: list[SongScore] = field(default_factory=list)
    review: list[SongScore] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    scores: dict[str, SongScore] = field(default_factory=dict)  # every song (not stored)
    excluded: set[str] = field(default_factory=set)
    weak: int = 0  # songs placed here only so that every song is in some playlist


@dataclass
class DraftResult:
    run_id: int
    drafts: list[PlaylistDraft]
    unassigned: list[str]
    overlaps: list[dict[str, Any]]
    untagged: int
    changes: dict[str, dict[str, int]] = field(default_factory=dict)  # name -> added/removed


# ---------------------------------------------------------------------------
# Library context
# ---------------------------------------------------------------------------


@dataclass
class Library:
    rids: list[str]
    index: dict[str, int]
    vectors: dict[str, dict[str, float]]
    raw: np.ndarray  # IDF-weighted rows over `tags` (not normalised)
    tags: list[str]
    label_cols: np.ndarray  # True where the tag is a genre / era label
    classes: vibes.TagClasses
    years: dict[str, int]
    evidence: dict[str, str]
    titles: dict[str, str]
    rec_of_uri: dict[str, str]
    idf: dict[str, float]  # tag specificity: rare tags count more than common ones


def load_library(db: Database, descriptors: dict[str, Descriptors] | None = None) -> Library:
    ensure_current(db)
    desc = descriptors if descriptors is not None else load_descriptors(db)
    classes = vibes.load_classes(db)
    titles: dict[str, str] = {}
    years: dict[str, int] = {}
    for r in db.conn.execute(
        """SELECT r.recording_id, r.title, r.artists_json, t.release_date
           FROM recordings r JOIN tracks t ON t.uri = r.primary_uri"""
    ):
        artists = ", ".join(json.loads(r["artists_json"]))
        titles[r["recording_id"]] = f"{r['title']} — {artists}" if artists else r["title"]
        d = r["release_date"]
        if d and d[:4].isdigit() and int(d[:4]) > 0:
            years[r["recording_id"]] = int(d[:4])
    rids = sorted(titles)
    vectors, evidence = {}, {}
    for rid in rids:
        d = desc.get(rid)
        prof = vibes.profile(d.by_source, classes) if d else None
        vectors[rid] = prof.merged if prof else {}
        evidence[rid] = prof.evidence if prof else "none"
        y = d.facts.get("first_release_year") if d else None
        if y and (rid not in years or y < years[rid]):
            years[rid] = y
    tags = sorted({t for v in vectors.values() for t in v})
    tindex = {t: i for i, t in enumerate(tags)}
    idf = tag_specificity(vectors)
    m = np.zeros((len(rids), max(len(tags), 1)))
    for i, rid in enumerate(rids):
        for t, w in vectors[rid].items():
            m[i, tindex[t]] = w * idf[t]
    label_cols = np.array([classes.is_label(t) for t in tags] or [False])
    rec_of_uri = {
        r[0]: r[1] for r in db.conn.execute("SELECT uri, recording_id FROM recording_members")
    }
    return Library(
        rids,
        {r: i for i, r in enumerate(rids)},
        vectors,
        m,
        tags,
        label_cols,
        classes,
        years,
        evidence,
        titles,
        rec_of_uri,
        idf,
    )


def playlist_matrix(lib: Library, rules: dict[str, Any]) -> np.ndarray:
    """Song vectors as this playlist sees them: vibe words, plus only the genre / era labels
    its definition asks for. Rows L2-normalised."""
    wanted = vibes.requested_labels(rules, lib.classes)
    mask = ~lib.label_cols.copy()
    if wanted:
        for i, t in enumerate(lib.tags):
            if t in wanted:
                mask[i] = True
    m = lib.raw * mask
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def tag_specificity(vectors: dict[str, dict[str, float]]) -> dict[str, float]:
    """IDF-style weight per tag: ln((N + 1) / (df + 1)) + 1, where df counts songs carrying the
    tag at weight ≥ 0.3. "rock" on 40 % of the library gets ~1.9; "nu metal" on 2 % gets ~4.9."""
    n = sum(1 for v in vectors.values() if v)
    df: dict[str, int] = defaultdict(int)
    for v in vectors.values():
        for t, w in v.items():
            if w >= TAG_PRESENT:
                df[t] += 1
    tags = {t for v in vectors.values() for t in v}
    return {t: math.log((n + 1) / (df.get(t, 0) + 1)) + 1 for t in tags}


def _examples(
    db: Database, identity_id: int, lib: Library, hide: set[str] | None = None
) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for uri, role in db.conn.execute(
        "SELECT uri, role FROM identity_examples WHERE identity_id=?", (identity_id,)
    ):
        if hide and uri in hide:
            continue
        rid = lib.rec_of_uri.get(uri)
        if rid:
            out[role].add(rid)
    return out


# ---------------------------------------------------------------------------
# Scoring one playlist
# ---------------------------------------------------------------------------


def _top_tag_reasons(vec: dict[str, float], weights: dict[str, float], n: int = 3) -> str:
    contrib = sorted(
        ((t, w * vec.get(t, 0.0)) for t, w in weights.items() if vec.get(t, 0.0) > 0),
        key=lambda x: -x[1],
    )[:n]
    return ", ".join(t for t, _ in contrib)


def _artist(lib: Library, rid: str) -> str:
    return lib.titles.get(rid, "").split(" — ", 1)[-1].split(",")[0].strip().lower()


def pick_auto_examples(
    db: Database, ident: Identity, lib: Library, *, k: int = AUTO_EXAMPLES
) -> set[str]:
    """Example songs the app picks itself when you've given fewer than 3.

    The songs whose own data best matches the playlist's description (tag score), judged by
    song-level data only, one per artist for variety, never a song you rejected there, and
    respecting required / avoided tags and years. Recomputed every draft, never stored; your
    own examples and decisions always come first."""
    if not ident.auto_maintain:
        return set()
    ex = _examples(db, ident.identity_id, lib)
    have = ex["reference"] | ex["essential"]
    if len(have) >= 3:
        return set()
    rules = ident.rules
    weights = {t: float(w) for t, w in (rules.get("tags") or {}).items()}
    if not weights:
        return set()
    eff = {t: w * lib.idf.get(t, 1.0) for t, w in weights.items()}
    wsum = sum(eff.values())
    avoid = set(rules.get("avoid_tags") or [])
    required = set(rules.get("require_tags") or [])
    years = rules.get("years")
    cands = []
    for rid in lib.rids:
        if rid in ex["exclude"] or rid in have or lib.evidence.get(rid) != "song":
            continue
        vec = lib.vectors[rid]
        if required and not any(vec.get(t, 0.0) >= TAG_PRESENT for t in required):
            continue
        if max((vec.get(t, 0.0) for t in avoid), default=0.0) >= AVOID_VETO:
            continue
        y = lib.years.get(rid)
        if years and y and not (years[0] <= y <= years[1]):
            continue
        ts = sum(w * vec.get(t, 0.0) for t, w in eff.items()) / wsum
        if ts >= AUTO_MIN_TAG_SCORE:
            cands.append((ts, rid))
    cands.sort(key=lambda x: (-x[0], x[1]))
    picked: list[str] = []
    artists = {_artist(lib, r) for r in have}
    for _ts, rid in cands:
        a = _artist(lib, rid)
        if a in artists:
            continue
        picked.append(rid)
        artists.add(a)
        if len(picked) + len(have) >= k:
            break
    return set(picked)


def score_playlist(
    db: Database,
    ident: Identity,
    lib: Library,
    *,
    previous: set[str] | None = None,
    hide: set[str] | None = None,
    auto_examples: set[str] | None = None,
) -> PlaylistDraft:
    """Score one playlist. ``previous`` = songs in its last draft (they get the lower exit
    bar, for stability); ``hide`` = example URIs to ignore (holdout evaluation);
    ``auto_examples`` = examples the app picked itself (``pick_auto_examples``)."""
    ex = _examples(db, ident.identity_id, lib, hide)
    refs, essential, excluded = ex["reference"], ex["essential"], ex["exclude"]
    auto = (auto_examples or set()) - excluded
    refs = refs | auto
    rules = ident.rules
    weights = {t: float(w) for t, w in (rules.get("tags") or {}).items()}
    avoid = set(rules.get("avoid_tags") or [])
    required = set(rules.get("require_tags") or [])
    years = rules.get("years")
    draft = PlaylistDraft(ident, threshold=None)

    if not ident.auto_maintain:  # hand-picked: exactly its examples
        for rid in sorted(refs | essential):
            draft.include.append(
                SongScore(
                    rid, "include", 1.0, None, None, lib.evidence.get(rid, "none"), ["hand-picked"]
                )
            )
        draft.notes.append("hand-picked: holds exactly its chosen songs")
        return draft

    eff = {t: w * lib.idf.get(t, 1.0) for t, w in weights.items()}
    wsum = sum(eff.values())
    mat = playlist_matrix(lib, rules)
    has_data = np.linalg.norm(mat, axis=1) > 0
    ref_rows = [lib.index[r] for r in refs if r in lib.index and has_data[lib.index[r]]]
    ref_matrix = mat[ref_rows] if ref_rows else None
    ref_ids = [lib.rids[i] for i in ref_rows]
    sims_all = mat @ ref_matrix.T if ref_matrix is not None else None
    # Songs you rejected act as negative examples for this playlist only
    rej_rows = [lib.index[r] for r in excluded if r in lib.index and has_data[lib.index[r]]]
    rej_ids = [lib.rids[i] for i in rej_rows]
    rej_sims = mat @ mat[rej_rows].T if rej_rows else None

    def raw(rid: str) -> SongScore:
        vec = lib.vectors[rid]
        reasons: list[str] = []
        if not vec:
            return SongScore(rid, "out", 0.0, None, None, "none", ["no tags known"])
        if required and not any(vec.get(t, 0.0) >= TAG_PRESENT for t in required):
            return SongScore(
                rid, "out", 0.0, None, None, lib.evidence[rid], ["missing required tag"]
            )
        tag_score = sum(w * vec.get(t, 0.0) for t, w in eff.items()) / wsum if wsum else None
        example_score = None
        if sims_all is not None:
            row = sims_all[lib.index[rid]]
            pairs = [(row[j], ref_ids[j]) for j in range(len(ref_ids)) if ref_ids[j] != rid]
            if pairs:
                pairs.sort(key=lambda x: -x[0])
                top = pairs[:TOP_K_EXAMPLES]
                example_score = float(np.mean([p[0] for p in top]))
                tag = " (auto-picked example)" if top[0][1] in auto else ""
                reasons.append(f"sounds like {lib.titles[top[0][1]]} ({top[0][0]:.2f}){tag}")
        parts = [x for x in (tag_score, example_score) if x is not None]
        score = sum(parts) / len(parts) if parts else 0.0
        if tag_score:
            top_tags = _top_tag_reasons(vec, eff)
            if top_tags:
                reasons.insert(0, f"tags: {top_tags}")
        bad = max((vec.get(t, 0.0) for t in avoid), default=0.0)
        if bad >= AVOID_VETO:
            hit = max(avoid, key=lambda t: vec.get(t, 0.0))
            return SongScore(
                rid,
                "out",
                0.0,
                tag_score,
                example_score,
                lib.evidence[rid],
                [f"has avoided tag '{hit}'"],
            )
        if bad > 0:
            score *= 1 - bad
            reasons.append("slightly matches an avoided tag")
        if rej_sims is not None and rid not in excluded:
            rrow = rej_sims[lib.index[rid]]
            j = int(np.argmax(rrow))
            best_rej = float(rrow[j])
            best_ref = (
                float(
                    max(
                        (
                            p
                            for p, r in zip(sims_all[lib.index[rid]], ref_ids, strict=True)
                            if r != rid
                        ),
                        default=0.0,
                    )
                )
                if sims_all is not None
                else 0.0
            )
            if best_rej >= REJECT_SIMILAR and best_rej > best_ref:
                score *= REJECT_PENALTY
                reasons.append(
                    f"sounds more like {lib.titles[rej_ids[j]]}, which you rejected here"
                )
        y = lib.years.get(rid)
        if years and y and not (years[0] <= y <= years[1]):
            score *= ERA_PENALTY
            reasons.append(f"{y} is outside {years[0]}–{years[1]}")
        elif years and y:
            reasons.append(f"{y} fits {years[0]}–{years[1]}")
        if lib.evidence[rid] == "artist":
            reasons.append("no data on the song itself: judged by its artist's tags")
        return SongScore(
            rid,
            "out",
            round(score, 4),
            None if tag_score is None else round(tag_score, 4),
            None if example_score is None else round(example_score, 4),
            lib.evidence[rid],
            reasons,
        )

    scores = {rid: raw(rid) for rid in lib.rids}
    draft.scores, draft.excluded = scores, set(excluded)

    ref_scores = [scores[r].score for r in refs if r in scores and scores[r].score > 0]
    if len(ref_scores) >= 3:
        ref_scores.sort()
        k = int(THRESHOLD_PERCENTILE * (len(ref_scores) - 1))
        threshold = min(THRESHOLD_MAX, max(THRESHOLD_MIN, ref_scores[k]))
    else:
        threshold = DEFAULT_THRESHOLD
        draft.notes.append("fewer than 3 usable example songs: using the default threshold")
    if auto:
        draft.notes.append(
            f"{len(auto)} example song(s) picked automatically from the description "
            "(your own picks always come first)"
        )
    # No playlist may take more than MAX_SHARE of the tagged library: a definition that broad
    # isn't a playlist identity. Raise the bar to the score at that share.
    tagged = sorted((s.score for s in scores.values() if s.score > 0), reverse=True)
    n_tagged = sum(1 for r in lib.rids if lib.vectors[r])
    cap_rank = int(MAX_SHARE * n_tagged)
    if cap_rank and len(tagged) > cap_rank and tagged[cap_rank] >= threshold:
        threshold = min(THRESHOLD_MAX, tagged[cap_rank] + 1e-6)
        draft.notes.append(
            f"definition is broad: capped at about {round(100 * MAX_SHARE)}% of your library; "
            "add more specific tags or examples to sharpen it"
        )
    draft.threshold = round(threshold, 3)
    if not weights and not ref_rows:
        draft.notes.append("no tags and no example songs: nothing to match against")

    previous = previous or set()
    exit_bar = threshold - REVIEW_BAND
    for rid, s in scores.items():
        if rid in excluded:
            continue
        if rid in essential:
            s.decision, s.reasons = "include", ["you marked this essential", *s.reasons]
            draft.include.append(s)
            continue
        if s.score >= threshold:
            s.decision = "include"
            draft.include.append(s)
        elif rid in previous and s.score >= exit_bar and s.score > 0:
            # Stability: a song already in the playlist only leaves when it falls clearly below
            s.decision = "include"
            s.reasons.append("kept from the last draft (stays unless it falls clearly below)")
            draft.include.append(s)
        elif s.score >= threshold - REVIEW_BAND and s.score > 0:
            s.decision = "review"
            draft.review.append(s)
    for bucket in (draft.include, draft.review):
        bucket.sort(key=lambda x: -x.score)
    dropped = [r for r in refs if scores.get(r) and scores[r].decision == "out"]
    if dropped:
        draft.notes.append(f"{len(dropped)} example song(s) didn't fit and were left out")
    return draft


# ---------------------------------------------------------------------------
# Whole collection
# ---------------------------------------------------------------------------


COVERAGE_KEY = "draft_coverage"


def coverage_enabled(db: Database) -> bool:
    return (db.get_meta(COVERAGE_KEY) or "on") == "on"


def set_coverage(db: Database, on: bool) -> None:
    db.set_meta(COVERAGE_KEY, "on" if on else "off")


def place_leftovers(
    lib: Library, drafts: list[PlaylistDraft], previous: dict[int, set[str]]
) -> None:
    """Put every song that cleared no bar into the playlist it comes closest to.

    "Closest" is judged within each playlist: how high the song ranks among all the songs
    that playlist scored (its percentile). A broad playlist with a long word list scores many
    songs a little, so comparing raw scores would hand it every leftover; ranking doesn't."""
    assigned = {s.recording_id for d in drafts for s in d.include}
    auto = [d for d in drafts if d.identity.auto_maintain and d.threshold]
    ranks: dict[int, dict[str, float]] = {}
    for d in auto:
        positive = sorted((s.score, r) for r, s in d.scores.items() if s.score > 0)
        n = len(positive)
        ranks[d.identity.identity_id] = {r: (i + 1) / n for i, (_sc, r) in enumerate(positive)}
    for rid in lib.rids:
        if rid in assigned:
            continue
        best: tuple[float, PlaylistDraft, SongScore] | None = None
        for d in auto:
            s = d.scores.get(rid)
            if s is None or s.score <= 0 or rid in d.excluded:
                continue
            rank = ranks[d.identity.identity_id].get(rid, 0.0)
            # tie-break on closeness to the bar; last draft's playlist gets a small edge
            key = rank + 0.01 * min(1.0, s.score / (d.threshold or 1.0))
            if rid in previous.get(d.identity.identity_id, set()):
                key += STICKY_RANK
            if best is None or key > best[0]:
                best = (key, d, s)
        if best is None:
            continue
        _ratio, d, s = best
        if s in d.review:
            d.review.remove(s)
        s.decision = "include"
        s.reasons = [WEAK_FIT, *s.reasons]
        d.include.append(s)
        d.weak += 1
    for d in auto:
        d.include.sort(key=lambda x: -x.score)


def build_drafts(
    db: Database,
    *,
    descriptors: dict[str, Descriptors] | None = None,
    coverage: bool | None = None,  # None = your saved setting (on unless you turned it off)
    auto_examples: bool = True,
) -> DraftResult:
    lib = load_library(db, descriptors)
    prev_run = latest_run(db)
    previous: dict[int, set[str]] = defaultdict(set)
    if prev_run is not None:
        for iid, rid in db.conn.execute(
            "SELECT identity_id, recording_id FROM draft_members "
            "WHERE run_id=? AND decision='include'",
            (prev_run,),
        ):
            previous[iid].add(rid)
    drafts = [
        score_playlist(
            db,
            ident,
            lib,
            previous=previous.get(ident.identity_id),
            auto_examples=pick_auto_examples(db, ident, lib) if auto_examples else None,
        )
        for ident in list_identities(db)
    ]
    if coverage if coverage is not None else coverage_enabled(db):
        place_leftovers(lib, drafts, previous)
    changes = {}
    if prev_run is not None:
        for d in drafts:
            now_in = {s.recording_id for s in d.include}
            before = previous.get(d.identity.identity_id, set())
            changes[d.identity.name] = {
                "added": len(now_in - before),
                "removed": len(before - now_in),
            }

    assigned = {s.recording_id for d in drafts for s in d.include}
    unassigned = [r for r in lib.rids if r not in assigned]
    overlaps = []
    members = {d.identity.identity_id: {s.recording_id for s in d.include} for d in drafts}
    names = {d.identity.identity_id: d.identity.name for d in drafts}
    ids = list(members)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if not members[a] or not members[b]:
                continue
            shared = len(members[a] & members[b])
            smaller = min(len(members[a]), len(members[b]))
            if shared and shared / smaller >= OVERLAP_REPORT:
                overlaps.append(
                    {
                        "a": names[a],
                        "b": names[b],
                        "shared": shared,
                        "share_of_smaller": round(shared / smaller, 2),
                    }
                )
    overlaps.sort(key=lambda x: (-x["share_of_smaller"], -x["shared"]))
    overlaps = overlaps[:MAX_OVERLAPS_REPORTED]

    summary = {
        "playlists": {
            d.identity.name: {
                "include": len(d.include),
                "review": len(d.review),
                "weak": d.weak,
                "threshold": d.threshold,
            }
            for d in drafts
        },
        "unassigned": len(unassigned),
        "recordings": len(lib.rids),
        "changes": changes,
    }
    with db.transaction():
        cur = db.conn.execute(
            "INSERT INTO draft_runs (created_at, summary_json) VALUES (?, ?)",
            (utcnow(), json.dumps(summary)),
        )
        run_id = int(cur.lastrowid or 0)
        db.conn.executemany(
            "INSERT INTO draft_members VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    d.identity.identity_id,
                    s.recording_id,
                    s.decision,
                    s.score,
                    s.tag_score,
                    s.example_score,
                    s.evidence,
                    json.dumps(s.reasons),
                )
                for d in drafts
                for s in d.include + d.review
            ],
        )
        # keep only the latest few runs (derived data)
        db.conn.execute(
            "DELETE FROM draft_runs WHERE run_id NOT IN "
            "(SELECT run_id FROM draft_runs ORDER BY run_id DESC LIMIT 5)"
        )
    untagged = sum(1 for r in lib.rids if not lib.vectors[r])
    return DraftResult(run_id, drafts, unassigned, overlaps, untagged, changes)


def latest_run(db: Database) -> int | None:
    row = db.conn.execute("SELECT MAX(run_id) FROM draft_runs").fetchone()
    return row[0] if row and row[0] else None


def load_draft(db: Database, identity_id: int, run_id: int | None = None) -> list[dict[str, Any]]:
    run_id = run_id or latest_run(db)
    if run_id is None:
        return []
    rows = db.conn.execute(
        """SELECT d.*, r.title, r.artists_json FROM draft_members d
           JOIN recordings r ON r.recording_id = d.recording_id
           WHERE d.run_id=? AND d.identity_id=? ORDER BY d.decision, d.score DESC""",
        (run_id, identity_id),
    ).fetchall()
    return [
        {
            "decision": r["decision"],
            "title": r["title"],
            "artists": ", ".join(json.loads(r["artists_json"])),
            "score": r["score"],
            "tag_score": r["tag_score"],
            "example_score": r["example_score"],
            "evidence": r["evidence"],
            "reasons": json.loads(r["reasons_json"]),
            "recording_id": r["recording_id"],
        }
        for r in rows
    ]
