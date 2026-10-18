"""Propose concepts for a new playlist collection, built from scratch.

Nothing here keeps, renames or edits an old playlist. Old playlists are evidence:
- Your **descriptions** of them (``curator intents``) name concepts you care about. Old
  playlists you described the same way (e.g. two "indie chill" lists) become one concept, and
  their songs become *example* songs for it (minus the ones the analysis marked as misfits).
- Your **library's tags** are clustered to find sounds no description covers.
- **Hand-picked** themes (e.g. a TV-show playlist) can come back as optional playlists the app
  never adds songs to automatically.
- Catch-alls and source-based lists (where a song came from) aren't concepts; their songs
  stay in the crate and can land in any new playlist.

Every proposal is stored with its evidence. You accept (→ an editable playlist identity),
or reject it. Re-running ``propose`` refreshes pending proposals and never resurrects ones
you already decided.

Clustering: spherical k-means on L2-normalised tag vectors (cosine geometry), k chosen by
mean silhouette, deterministic seed. Thresholds are documented heuristics, not calibrated.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from playlist_curator.crate.deduplication import ensure_current
from playlist_curator.curator.analysis import CollectionReport, analyze, centroid, cosine
from playlist_curator.curator.intents import CONCEPT_KINDS, HANDPICKED_KINDS
from playlist_curator.metadata.descriptors import Descriptors, load_descriptors
from playlist_curator.storage.database import Database, utcnow

MIN_TAG_RECORDINGS = 3  # a tag must appear on this many songs to be a clustering dimension
CLUSTER_K_RANGE = range(6, 19)
CLUSTER_MIN_SIZE = 15
CLUSTER_SEED = 42
COVERED_BY_DESCRIPTION = 0.80  # cluster this similar to a described concept is "covered"
SIMILAR_CONCEPTS = 0.85
TOP_RULE_TAGS = 8
MAX_EXAMPLES_FROM_CLUSTER = 25


@dataclass
class ConceptDraft:
    name: str
    kind: str
    concept: str
    auto_maintain: bool
    rules: dict[str, Any]
    reference_uris: list[str] = field(default_factory=list)


@dataclass
class Proposal:
    action: str  # from_description | from_library | handpicked
    title: str
    playlist_ids: list[str]
    evidence: dict[str, Any]
    draft: ConceptDraft
    proposal_id: int | None = None
    status: str = "pending"

    @property
    def signature(self) -> str:
        if self.action == "from_library":
            return "lib:" + ",".join(sorted(t["tag"] for t in self.evidence["top_tags"][:3]))
        return f"{self.action}:" + ",".join(sorted(self.playlist_ids))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def years_from_text(text: str) -> list[int] | None:
    """Decades mentioned in a description: "70-80-90" -> [1970, 1999]; "80s" -> [1980, 1989]."""
    decades = []
    for m in re.finditer(r"(?<![\d.])(?:19|20)?([0-9])0(?:'?s)?(?![\d])", text):
        d = int(m.group(1))
        decades.append(1900 + d * 10 if d >= 3 else 2000 + d * 10)
    if not decades:
        return None
    return [min(decades), max(decades) + 9]


def tag_rules(vectors: list[dict[str, float]], crate_mean: dict[str, float]) -> list[dict]:
    """Tags that are common in a group *and* more common than in the whole crate."""
    if not vectors:
        return []
    prof = centroid(vectors)
    rows = []
    for t, mean_w in prof.items():
        support = sum(1 for v in vectors if v.get(t, 0) >= 0.3) / len(vectors)
        if support < 0.25:
            continue
        lift = mean_w / crate_mean[t] if crate_mean.get(t) else 1.0
        rows.append(
            {
                "tag": t,
                "support": round(support, 3),
                "lift": round(lift, 2),
                "score": support * math.log2(1 + lift),
            }
        )
    rows.sort(key=lambda r: -r["score"])
    return [{k: v for k, v in r.items() if k != "score"} for r in rows[:TOP_RULE_TAGS]]


def _crate_mean(vectors: dict[str, dict[str, float]]) -> dict[str, float]:
    total: dict[str, float] = defaultdict(float)
    for v in vectors.values():
        for t, w in v.items():
            total[t] += w
    n = max(len(vectors), 1)
    return {t: w / n for t, w in total.items()}


def _year_span(years: list[int]) -> dict[str, Any] | None:
    if not years:
        return None
    ys = sorted(years)
    return {
        "median": ys[len(ys) // 2],
        "p10": ys[int(0.1 * (len(ys) - 1))],
        "p90": ys[int(0.9 * (len(ys) - 1))],
    }


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _matrix(vectors: dict[str, dict[str, float]]) -> tuple[list[str], list[str], np.ndarray]:
    counts: dict[str, int] = defaultdict(int)
    for v in vectors.values():
        for t in v:
            counts[t] += 1
    tags = sorted(t for t, c in counts.items() if c >= MIN_TAG_RECORDINGS)
    index = {t: i for i, t in enumerate(tags)}
    rids = [r for r, v in vectors.items() if any(t in index for t in v)]
    x = np.zeros((len(rids), len(tags)))
    for i, r in enumerate(rids):
        for t, w in vectors[r].items():
            j = index.get(t)
            if j is not None:
                x[i, j] = w
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return rids, tags, x / norms


def spherical_kmeans(
    x: np.ndarray, k: int, seed: int = CLUSTER_SEED, iters: int = 50, n_init: int = 4
) -> tuple[np.ndarray, np.ndarray]:
    """k-means on the unit sphere (cosine similarity). Deterministic for a given seed."""
    rng = np.random.default_rng(seed)
    best: tuple[float, np.ndarray, np.ndarray] | None = None
    for _ in range(n_init):
        # k-means++ style seeding on cosine distance
        centers = [x[rng.integers(len(x))]]
        for _ in range(1, k):
            d = 1 - np.max(x @ np.array(centers).T, axis=1)
            d = np.clip(d, 0, None)
            probs = d / d.sum() if d.sum() > 0 else None
            centers.append(x[rng.choice(len(x), p=probs)])
        c = np.array(centers)
        labels = np.zeros(len(x), dtype=int)
        for _ in range(iters):
            new = np.argmax(x @ c.T, axis=1)
            if np.array_equal(new, labels) and _ > 0:
                break
            labels = new
            for j in range(k):
                members = x[labels == j]
                if len(members):
                    m = members.sum(axis=0)
                    n = np.linalg.norm(m)
                    c[j] = m / n if n else c[j]
        inertia = float(np.sum(np.max(x @ c.T, axis=1)))
        if best is None or inertia > best[0]:
            best = (inertia, labels.copy(), c.copy())
    assert best is not None
    return best[1], best[2]


def silhouette(x: np.ndarray, labels: np.ndarray) -> float:
    """Mean silhouette with cosine distance."""
    k = labels.max() + 1
    if k < 2:
        return 0.0
    dist = 1 - x @ x.T
    member = np.eye(k)[labels]  # n x k
    sizes = member.sum(axis=0)
    sums = dist @ member  # n x k: total distance to each cluster
    own = sizes[labels]
    a = np.where(own > 1, sums[np.arange(len(x)), labels] / np.maximum(own - 1, 1), 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_to = sums / sizes
    mean_to[np.arange(len(x)), labels] = np.inf
    mean_to[:, sizes == 0] = np.inf
    b = mean_to.min(axis=1)
    s = np.where(own > 1, (b - a) / np.maximum(a, b), 0.0)
    return float(np.nanmean(s))


@dataclass
class Cluster:
    recording_ids: list[str]
    centroid: dict[str, float]
    top_tags: list[dict]
    closest: list[str]  # recording ids nearest the centre


def discover_clusters(
    vectors: dict[str, dict[str, float]], crate_mean: dict[str, float]
) -> tuple[list[Cluster], dict[str, Any]]:
    rids, tags, x = _matrix(vectors)
    info: dict[str, Any] = {"tagged_songs": len(rids), "dimensions": len(tags)}
    if len(rids) < CLUSTER_MIN_SIZE * 2:
        info["skipped"] = "not enough tagged songs to cluster"
        return [], info
    best = None
    for k in CLUSTER_K_RANGE:
        if k >= len(rids) // CLUSTER_MIN_SIZE:
            break
        labels, centers = spherical_kmeans(x, k)
        score = silhouette(x, labels)
        if best is None or score > best[0]:
            best = (score, k, labels, centers)
    if best is None:
        info["skipped"] = "library too small for the cluster range"
        return [], info
    score, k, labels, centers = best
    info.update({"k": k, "silhouette": round(score, 3)})
    clusters = []
    for j in range(k):
        idx = np.where(labels == j)[0]
        if len(idx) < CLUSTER_MIN_SIZE:
            continue
        members = [rids[i] for i in idx]
        sims = x[idx] @ centers[j]
        order = idx[np.argsort(-sims)]
        clusters.append(
            Cluster(
                recording_ids=members,
                centroid=centroid([vectors[r] for r in members]),
                top_tags=tag_rules([vectors[r] for r in members], crate_mean),
                closest=[rids[i] for i in order[:MAX_EXAMPLES_FROM_CLUSTER]],
            )
        )
    return clusters, info


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


def _load_context(db: Database) -> dict[str, Any]:
    ensure_current(db)
    members: dict[str, set[str]] = defaultdict(set)
    for r in db.conn.execute(
        """SELECT se.source_id, rm.recording_id FROM source_entries se
           JOIN recording_members rm ON rm.uri = se.track_uri WHERE se.kind='playlist'"""
    ):
        members[r[0]].add(r[1])
    primary = {
        r[0]: r[1] for r in db.conn.execute("SELECT recording_id, primary_uri FROM recordings")
    }
    years: dict[str, int] = {}
    for r in db.conn.execute(
        "SELECT r.recording_id, t.release_date FROM recordings r "
        "JOIN tracks t ON t.uri = r.primary_uri"
    ):
        if r[1] and r[1][:4].isdigit() and int(r[1][:4]) > 0:
            years[r[0]] = int(r[1][:4])
    liked = {
        r[0]
        for r in db.conn.execute(
            """SELECT DISTINCT rm.recording_id FROM source_entries se
               JOIN recording_members rm ON rm.uri = se.track_uri WHERE se.kind='liked'"""
        )
    }
    names = {r["spotify_id"]: r["name"] for r in db.list_playlists()}
    intents = db.conn.execute("SELECT * FROM playlist_intents").fetchall()
    return {
        "members": members,
        "primary": primary,
        "years": years,
        "liked": liked,
        "names": names,
        "intents": intents,
    }


def propose(
    db: Database,
    *,
    descriptors: dict[str, Descriptors] | None = None,
    report: CollectionReport | None = None,
) -> tuple[list[Proposal], dict[str, Any]]:
    desc = descriptors if descriptors is not None else load_descriptors(db)
    report = report if report is not None else analyze(db, descriptors=desc)
    ctx = _load_context(db)
    vectors = {rid: d.combined for rid, d in desc.items() if d.by_source}
    for rid, d in desc.items():  # prefer MusicBrainz original year when known
        y = d.facts.get("first_release_year")
        if y and (rid not in ctx["years"] or y < ctx["years"][rid]):
            ctx["years"][rid] = y
    crate_mean = _crate_mean(vectors)
    outliers = {p.playlist_id: {o.recording_id for o in p.outliers} for p in report.playlists}

    proposals: list[Proposal] = []
    concept_profiles: list[tuple[Proposal, dict[str, float]]] = []

    # 1. Concepts from your descriptions, grouped by title ("Indie chill" twice -> one concept)
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for it in ctx["intents"]:
        if it["kind"] in CONCEPT_KINDS + HANDPICKED_KINDS:
            groups[(it["kind"] in HANDPICKED_KINDS, it["title"].strip().casefold())].append(it)

    for (handpicked, _), items in sorted(groups.items(), key=lambda g: g[1][0]["title"].casefold()):
        pids = sorted({i["playlist_id"] for i in items})
        songs: set[str] = set()
        for pid in pids:
            songs |= ctx["members"].get(pid, set())
        examples = (
            songs
            if handpicked
            else {
                r
                for pid in pids
                for r in ctx["members"].get(pid, set())
                if r not in outliers.get(pid, set())
            }
        )
        tagged = [vectors[r] for r in examples if r in vectors]
        rules_tags = tag_rules(tagged, crate_mean)
        text = " ".join(f"{i['title']} {i['intent']}" for i in items)
        yrs = years_from_text(text)
        kind = "handpicked" if handpicked else items[0]["kind"]
        title = items[0]["title"]
        concept = " / ".join(dict.fromkeys(i["intent"] for i in items))
        evidence = {
            "from_playlists": [
                {
                    "id": pid,
                    "name": ctx["names"].get(pid, pid),
                    "songs": len(ctx["members"].get(pid, set())),
                    "misfits_left_out": len(
                        outliers.get(pid, set()) & ctx["members"].get(pid, set())
                    ),
                }
                for pid in pids
            ],
            "example_songs": len(examples),
            "tagged_examples": len(tagged),
            "top_tags": rules_tags,
            "years": _year_span([ctx["years"][r] for r in examples if r in ctx["years"]]),
            "years_from_description": yrs,
            "liked_share": round(len(examples & ctx["liked"]) / len(examples), 3)
            if examples
            else 0,
        }
        draft = ConceptDraft(
            name=title,
            kind=kind,
            concept=concept,
            auto_maintain=not handpicked,
            rules={
                "tags": {t["tag"]: t["support"] for t in rules_tags},
                "avoid_tags": [],
                "years": yrs,
                "years_mode": "prefer",
            },
            reference_uris=sorted(ctx["primary"][r] for r in examples if r in ctx["primary"]),
        )
        prop = Proposal(
            "handpicked" if handpicked else "from_description", title, pids, evidence, draft
        )
        proposals.append(prop)
        if tagged and not handpicked:
            concept_profiles.append((prop, centroid(tagged)))

    # Flag described concepts that look alike (you decide whether both are worth having)
    for i, (a, pa) in enumerate(concept_profiles):
        for b, pb in concept_profiles[i + 1 :]:
            sim = cosine(pa, pb)
            if sim >= SIMILAR_CONCEPTS:
                a.evidence.setdefault("similar_to", []).append(
                    {"title": b.title, "sim": round(sim, 2)}
                )
                b.evidence.setdefault("similar_to", []).append(
                    {"title": a.title, "sim": round(sim, 2)}
                )

    # 2. Concepts from your library's tags that no description covers
    clusters, cluster_info = discover_clusters(vectors, crate_mean)
    for cl in clusters:
        best = max(
            ((p, cosine(cl.centroid, prof)) for p, prof in concept_profiles),
            key=lambda x: x[1],
            default=(None, 0.0),
        )
        if best[0] is not None and best[1] >= COVERED_BY_DESCRIPTION:
            best[0].evidence.setdefault("library_clusters", []).append(
                {
                    "songs": len(cl.recording_ids),
                    "sim": round(best[1], 2),
                    "tags": [t["tag"] for t in cl.top_tags[:3]],
                }
            )
            continue
        if not cl.top_tags:
            continue
        label = " / ".join(t["tag"] for t in cl.top_tags[:2])
        title = label.title()
        evidence = {
            "cluster_songs": len(cl.recording_ids),
            "top_tags": cl.top_tags,
            "years": _year_span([ctx["years"][r] for r in cl.recording_ids if r in ctx["years"]]),
            "liked_share": round(
                len(set(cl.recording_ids) & ctx["liked"]) / len(cl.recording_ids), 3
            ),
            "closest_to_description": (
                {"title": best[0].title, "sim": round(best[1], 2)} if best[0] else None
            ),
        }
        draft = ConceptDraft(
            name=title,
            kind="sound",
            concept=f"Songs whose tags centre on {label}.",
            auto_maintain=True,
            rules={
                "tags": {t["tag"]: t["support"] for t in cl.top_tags},
                "avoid_tags": [],
                "years": None,
                "years_mode": "prefer",
            },
            reference_uris=sorted(ctx["primary"][r] for r in cl.closest if r in ctx["primary"]),
        )
        proposals.append(Proposal("from_library", title, [], evidence, draft))

    return proposals, cluster_info


# ---------------------------------------------------------------------------
# Persistence and decisions
# ---------------------------------------------------------------------------


def save_proposals(db: Database, proposals: list[Proposal]) -> list[Proposal]:
    """Replace pending proposals; skip any you already accepted or rejected."""
    decided = {
        r[0]
        for r in db.conn.execute(
            "SELECT signature FROM proposals WHERE status IN ('accepted','rejected')"
        )
    }
    kept = [p for p in proposals if p.signature not in decided]
    with db.transaction():
        db.conn.execute("DELETE FROM proposals WHERE status='pending'")
        for p in kept:
            cur = db.conn.execute(
                """INSERT INTO proposals (action, title, playlists_json, evidence_json, drafts_json,
                       status, signature, created_at) VALUES (?,?,?,?,?, 'pending', ?, ?)""",
                (
                    p.action,
                    p.title,
                    json.dumps(p.playlist_ids),
                    json.dumps(p.evidence),
                    json.dumps(asdict(p.draft)),
                    p.signature,
                    utcnow(),
                ),
            )
            p.proposal_id = int(cur.lastrowid or 0)
    return kept


def load_proposals(db: Database, *, include_decided: bool = False) -> list[Proposal]:
    sql = "SELECT * FROM proposals"
    if not include_decided:
        sql += " WHERE status='pending'"
    sql += (
        " ORDER BY CASE action WHEN 'from_description' THEN 0 "
        "WHEN 'from_library' THEN 1 ELSE 2 END, title"
    )
    out = []
    for r in db.conn.execute(sql):
        out.append(
            Proposal(
                action=r["action"],
                title=r["title"],
                playlist_ids=json.loads(r["playlists_json"]),
                evidence=json.loads(r["evidence_json"]),
                draft=ConceptDraft(**json.loads(r["drafts_json"])),
                proposal_id=r["proposal_id"],
                status=r["status"],
            )
        )
    return out


def accept(db: Database, proposal_id: int, *, name: str | None = None) -> int:
    """Turn a proposal into a playlist identity you can edit. Returns the identity id."""
    row = db.conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    if row is None:
        raise KeyError(f"No proposal {proposal_id}")
    if row["status"] != "pending":
        raise ValueError(f"Proposal {proposal_id} is already {row['status']}")
    draft = ConceptDraft(**json.loads(row["drafts_json"]))
    now = utcnow()
    with db.transaction():
        cur = db.conn.execute(
            """INSERT INTO identities (name, kind, concept, status, auto_maintain, rules_json,
                   origin_json, created_at, updated_at) VALUES (?,?,?,'approved',?,?,?,?,?)""",
            (
                name or draft.name,
                draft.kind,
                draft.concept,
                int(draft.auto_maintain),
                json.dumps(draft.rules),
                json.dumps(
                    {"proposal_id": proposal_id, "playlists": json.loads(row["playlists_json"])}
                ),
                now,
                now,
            ),
        )
        identity_id = int(cur.lastrowid or 0)
        db.conn.executemany(
            "INSERT OR IGNORE INTO identity_examples VALUES (?,?,?,?,?)",
            [(identity_id, uri, "reference", "seed", now) for uri in draft.reference_uris],
        )
        db.conn.execute(
            "UPDATE proposals SET status='accepted', decided_at=? WHERE proposal_id=?",
            (now, proposal_id),
        )
    return identity_id


def reject(db: Database, proposal_id: int) -> None:
    cur = db.conn.execute(
        "UPDATE proposals SET status='rejected', decided_at=? "
        "WHERE proposal_id=? AND status='pending'",
        (utcnow(), proposal_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"No pending proposal {proposal_id}")


def reopen(db: Database, proposal_id: int) -> None:
    """Undo a rejection so the proposal can be reconsidered."""
    db.conn.execute(
        "UPDATE proposals SET status='pending', decided_at=NULL "
        "WHERE proposal_id=? AND status='rejected'",
        (proposal_id,),
    )
