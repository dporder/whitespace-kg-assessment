"""Concept resolution: term collisions first, then near-duplicate merging.

SPEC 2.4, in order, because the order is the point:

1. **A concept that collides with a declared Term is not minted.** Exact label
   match, alias match, or embedding near-duplicate above
   `config.CONCEPT_MERGE_COSINE`. The collision is logged and the term keeps the
   job, because a deterministic tier 2 object always outranks a generated tier 3
   one (DESIGN tier 3).
2. **Near duplicates collapse** by embedding cosine at the same threshold, with
   a merge log kept.

**Where the declared vocabulary comes from.** SPEC 3 says stages 3 to 6 never
read each other's output, and SPEC 2.4 says stage 5 must check its labels
against declared Terms. Both hold at once by deriving the declared list here
from the *trees*, using `pipeline/vocabulary/declared.py` as a library, rather
than reading stage 4's `definition_sites.json`. Declared ingestion is a pure
function of the trees, so the two stages agree by construction and can still run
in parallel.

**When vectors are unavailable.** The cosine check needs embeddings. Rather than
skip resolution entirely, a strictly weaker lexical check runs and says so:
identical normalised labels, and one label's words wholly containing the other's.
Every pair it could not compare by cosine is listed, so nobody reads a lexical
merge log as a cosine one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import config
from pipeline.concepts.scan import ProposedConcept, concept_id, normalise_label
from pipeline.embeddings.client import Embedder, cosine, load_vector
from pipeline.schemas import Concept, ConceptRelation
from pipeline.vocabulary import declared as declared_mod
from pipeline.vocabulary import treeio

COSINE = "embedding_cosine"
LEXICAL = "lexical_fallback_pending_embeddings"


@dataclass
class TermCollision:
    label: str
    scope_path: str
    term: str
    how: str                       # exact | alias | embedding_near_duplicate
    score: Optional[float] = None

    def as_dict(self) -> dict:
        return {"proposed_label": self.label, "scope_path": self.scope_path,
                "collides_with_term": self.term, "how": self.how,
                "score": self.score,
                "ruling": "not minted; a declared Term outranks a generated concept"}


@dataclass
class Merge:
    kept: str
    absorbed: str
    method: str
    score: Optional[float] = None

    def as_dict(self) -> dict:
        return {"kept": self.kept, "absorbed": self.absorbed,
                "method": self.method, "score": self.score}


@dataclass
class Resolution:
    concepts: list[Concept] = field(default_factory=list)
    collisions: list[TermCollision] = field(default_factory=list)
    merges: list[Merge] = field(default_factory=list)
    method: str = COSINE
    uncompared_pairs: int = 0
    note: str = ""
    proposed_count: int = 0

    def as_dict(self) -> dict:
        return {
            "proposed": self.proposed_count,
            "minted": len(self.concepts),
            "not_minted_term_collision": len(self.collisions),
            "merged_away": len(self.merges),
            "resolution_method": self.method,
            "merge_threshold": config.CONCEPT_MERGE_COSINE,
            "pairs_not_compared_by_cosine": self.uncompared_pairs,
            "note": self.note,
            "collisions": [c.as_dict() for c in self.collisions],
            "merge_log": [m.as_dict() for m in self.merges],
        }


# ------------------------------------------------------------ declared terms


def declared_surfaces(trees: treeio.Trees) -> dict[str, str]:
    """normalised surface -> the term it belongs to, terms and aliases alike.

    Derived from the trees by the same code stage 4 uses, never from stage 4's
    output file.
    """
    out: dict[str, str] = {}
    for site in declared_mod.ingest(trees, config.BATCHES):
        for surface in [site.term, *site.aliases]:
            key = normalise_label(surface)
            if key:
                out.setdefault(key, site.term)
    return out


# ------------------------------------------------------------------ vectors


class Vectors:
    """Lazy embedding lookup for concept labels, backed by the stage 6 cache."""

    def __init__(self, embedder: Optional[Embedder]):
        self.embedder = embedder
        self._cache: dict[str, list[float]] = {}
        self.available = embedder is not None
        self.note = ("" if embedder is not None else
                     "no embedder supplied; resolution fell back to the lexical check")

    def warm(self, texts: list[str]) -> None:
        if self.embedder is None:
            return
        result = self.embedder.embed(texts)
        if result.missing and not result.vectors:
            self.available = False
            self.note = (f"embeddings unavailable ({sorted(set(result.missing.values()))}); "
                         f"resolution fell back to the lexical check: {result.note}")
            return
        root: Path = self.embedder.output_root
        for text, ref in result.vectors.items():
            vector = load_vector(root, ref)
            if vector is not None:
                self._cache[text] = vector
        if result.missing:
            self.note = (f"{len(result.missing)} label(s) have no vector; those pairs "
                         f"fell back to the lexical check")

    def get(self, text: str) -> Optional[list[float]]:
        return self._cache.get(text)

    def best_match(self, text: str, candidates: list[str]
                   ) -> Optional[tuple[str, float]]:
        """The closest candidate by cosine, or None when either side has no
        vector. Vectorised where numpy is available, because the declared
        vocabulary runs to hundreds of terms and 3072 dimensions and the naive
        double loop is minutes of Python for a question numpy answers in
        milliseconds."""
        vector = self.get(text)
        if vector is None:
            return None
        pool = [(c, self.get(c)) for c in candidates]
        pool = [(c, v) for c, v in pool if v is not None]
        if not pool:
            return None
        try:
            import numpy as np                             # noqa: PLC0415
        except Exception:                                  # noqa: BLE001
            scored = [(c, cosine(vector, v)) for c, v in pool]
            return max(scored, key=lambda cs: (cs[1], cs[0]))
        matrix = np.asarray([v for _c, v in pool], dtype="float32")
        query = np.asarray(vector, dtype="float32")
        norms = np.linalg.norm(matrix, axis=1) * float(np.linalg.norm(query))
        with np.errstate(divide="ignore", invalid="ignore"):
            scores = np.where(norms > 0, matrix @ query / norms, 0.0)
        best = int(np.argmax(scores))
        return pool[best][0], float(scores[best])


def _lexical_similar(a: str, b: str) -> bool:
    """Deliberately weaker than cosine, and never described as more.

    Identical normalised labels, or one label's words wholly containing the
    other's ("termination triggers" inside "supplier termination triggers").
    """
    if a == b:
        return True
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return False
    return wa <= wb or wb <= wa


def similarity(a: str, b: str, vectors: Vectors) -> tuple[Optional[float], str]:
    va, vb = vectors.get(a), vectors.get(b)
    if va is not None and vb is not None:
        return cosine(va, vb), COSINE
    return (1.0 if _lexical_similar(a, b) else 0.0), LEXICAL


# ----------------------------------------------------------------- union-find


class _Union:
    def __init__(self, keys):
        self.parent = {k: k for k in keys}

    def find(self, k):
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ------------------------------------------------------------------ resolve


def resolve(proposed: list[ProposedConcept], trees: treeio.Trees,
            embedder: Optional[Embedder] = None,
            threshold: float = None,
            surfaces: Optional[dict[str, str]] = None) -> Resolution:
    threshold = config.CONCEPT_MERGE_COSINE if threshold is None else threshold
    result = Resolution(proposed_count=len(proposed))
    if not proposed:
        result.note = "nothing proposed"
        return result

    surfaces = declared_surfaces(trees) if surfaces is None else surfaces
    vectors = Vectors(embedder)
    labels = sorted({normalise_label(p.label) for p in proposed} | set(surfaces))
    vectors.warm(labels)
    result.method = COSINE if vectors.available else LEXICAL
    result.note = vectors.note

    # 1. Term collisions: tier 2 outranks tier 3, so these are never minted.
    surface_keys = sorted(surfaces)
    survivors: list[ProposedConcept] = []
    for concept in proposed:
        key = normalise_label(concept.label)
        if key in surfaces:
            result.collisions.append(TermCollision(
                label=concept.label, scope_path=concept.scope_path,
                term=surfaces[key], how="exact_or_alias", score=1.0))
            continue
        hit = None
        best = vectors.best_match(key, surface_keys)
        if best is not None and best[1] >= threshold:
            hit = TermCollision(label=concept.label, scope_path=concept.scope_path,
                                term=surfaces[best[0]], how="embedding_near_duplicate",
                                score=round(best[1], 4))
        elif best is None:
            for surface, term in surfaces.items():
                if _lexical_similar(key, surface):
                    hit = TermCollision(label=concept.label,
                                        scope_path=concept.scope_path, term=term,
                                        how="lexical_near_duplicate", score=1.0)
                    break
        if hit is not None:
            result.collisions.append(hit)
            continue
        survivors.append(concept)

    # 2. Near-duplicate merging, union-find so a chain collapses to one cluster.
    keys = [c.id for c in survivors]
    by_id = {c.id: c for c in survivors}
    union = _Union(keys)
    uncompared = 0
    for i, left in enumerate(survivors):
        for right in survivors[i + 1:]:
            score, method = similarity(normalise_label(left.label),
                                       normalise_label(right.label), vectors)
            if method == LEXICAL and vectors.available:
                uncompared += 1
            if score is not None and score >= threshold:
                keep, absorb = sorted(
                    (left, right), key=lambda c: (-c.confidence, c.label, c.id))
                result.merges.append(Merge(kept=keep.label, absorbed=absorb.label,
                                           method=method, score=round(score, 4)))
                union.union(keep.id, absorb.id)
    result.uncompared_pairs = uncompared

    clusters: dict[str, list[ProposedConcept]] = {}
    for key in keys:
        clusters.setdefault(union.find(key), []).append(by_id[key])

    for root_id in sorted(clusters):
        members = sorted(clusters[root_id],
                         key=lambda c: (-c.confidence, c.label, c.id))
        head = members[0]
        node_ids: list[str] = []
        relations: list[ConceptRelation] = []
        for member in members:
            for nid in member.member_node_ids:
                if nid not in node_ids:
                    node_ids.append(nid)
        # The highest altitude wins the scope: the shortest path among the
        # merged, which is the unit that saw the most context.
        scope_path = min((m.scope_path for m in members), key=lambda p: (len(p), p))
        cid = concept_id(scope_path, head.label)
        seen: set[tuple[str, str, str]] = set()
        for member in members:
            for relation in member.relations:
                dst = concept_id(member.scope_path, relation["to"])
                key3 = (cid, relation["label"], dst)
                if dst != cid and key3 not in seen:
                    seen.add(key3)
                    relations.append(ConceptRelation(src=cid, label=relation["label"],
                                                     dst=dst))
        result.concepts.append(Concept(
            id=cid, label=head.label, scope_path=scope_path,
            member_node_ids=node_ids, relations=relations,
            llm_derived=True, confidence=head.confidence))

    result.concepts.sort(key=lambda c: (c.scope_path, c.label))
    return result
