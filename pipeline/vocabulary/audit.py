"""Stratified audit sampling of the confident matches.

DESIGN tier 2: "Routing only the self declared hard cases is not enough, because
a systematic error in the easy cases would run silently. So a stratified random
sample of confident matches is audited as well, stratified by term length,
document part and match position so the sample matches the population rather
than the convenient cases."

Drawn exactly per `config.AUDIT`: `confident_term_sample_size` items, strata
`term_word_count`, `part`, `position`. Proportional allocation with largest
remainder, ties broken by cell key, a seeded shuffle inside each cell, and a seed
derived from the population itself, so the same population always yields the same
sample and no wall clock or global RNG is involved.

**Why the sampler is reimplemented here rather than imported.** Stage 8 has one
in `pipeline/eval/sampling.py`, and stage 4 importing it would make the
enrichment stage depend on the evaluation stage, inverting the pipeline. So the
algorithm is written out again, and `tests/vocabulary/test_audit.py` asserts the
two implementations return *identical* samples for the same population, which is
the anti-drift pattern this repo already uses for the golden verdict vocabulary.
If eval-builder changes the algorithm, that test fails rather than the two
silently disagreeing about what was audited.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence, TypeVar

from pipeline.vocabulary.matching import Match
from pipeline.vocabulary.text import position_bucket, word_count_bucket

T = TypeVar("T")

POPULATION_NAME = "confident_term_uses"


@dataclass
class Stratum:
    key: tuple
    population: int
    allocated: int
    taken: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"stratum": " | ".join(str(k) for k in self.key),
                "population": self.population, "sampled": len(self.taken)}


@dataclass
class SampleResult:
    population_size: int
    requested: int
    indices: list[int] = field(default_factory=list)
    strata: list[Stratum] = field(default_factory=list)
    strata_names: list[str] = field(default_factory=list)
    seed: str = ""

    @property
    def size(self) -> int:
        return len(self.indices)

    def as_dict(self) -> dict[str, Any]:
        return {"strata": self.strata_names, "population_size": self.population_size,
                "requested_sample_size": self.requested, "drawn_sample_size": self.size,
                "cells": [s.as_dict() for s in self.strata], "seed": self.seed}


def _seed_for(material: str) -> int:
    return int(hashlib.sha1(material.encode()).hexdigest()[:16], 16)


def stratified_sample(population: Sequence[T], stratifier: Callable[[T], tuple],
                      size: int, strata_names: Iterable[str],
                      seed_material: str = "") -> SampleResult:
    n = len(population)
    result = SampleResult(population_size=n, requested=size,
                          strata_names=list(strata_names))
    if n == 0 or size <= 0:
        return result

    cells: dict[tuple, list[int]] = {}
    for i, item in enumerate(population):
        cells.setdefault(tuple(stratifier(item)), []).append(i)
    keys = sorted(cells, key=lambda k: tuple(str(x) for x in k))

    material = seed_material + "|" + "|".join(f"{k}:{len(cells[k])}" for k in keys)
    result.seed = hashlib.sha1(material.encode()).hexdigest()[:16]

    if size >= n:                                   # take everything, say so
        for k in keys:
            result.strata.append(Stratum(key=k, population=len(cells[k]),
                                         allocated=len(cells[k]), taken=sorted(cells[k])))
        result.indices = sorted(range(n))
        return result

    exact = {k: size * len(cells[k]) / n for k in keys}
    base = {k: int(exact[k]) for k in keys}
    remaining = size - sum(base.values())
    order = sorted(keys, key=lambda k: (-(exact[k] - base[k]), tuple(str(x) for x in k)))
    for k in order[:remaining]:
        base[k] += 1

    for k in keys:
        idxs = list(cells[k])
        rng = random.Random(_seed_for(f"{material}#{k}"))
        rng.shuffle(idxs)
        take = sorted(idxs[:base[k]])
        result.strata.append(Stratum(key=k, population=len(cells[k]),
                                     allocated=base[k], taken=take))
        result.indices.extend(take)
    result.indices.sort()
    return result


# ------------------------------------------------------------- population


def population(matches: list[Match], orders: dict[str, tuple[int, int]]
               ) -> list[dict[str, Any]]:
    """The confident matches, shaped as `pipeline/eval/sections/stratified_audit.py`
    shapes them, so a checker sees the same item whichever side drew it."""
    out: list[dict[str, Any]] = []
    for m in matches:
        if m.status != "confident":
            continue
        order, total = orders.get(m.node_id, (m.order, m.order))
        out.append({
            "kind": "term_use", "term": m.term, "node_id": m.node_id,
            "path": m.node_path, "char_span": list(m.span), "surface": m.surface,
            "sentence": m.sentence, "part": m.part,
            "term_word_count": word_count_bucket(m.term),
            "position": position_bucket(order, total),
        })
    return out


def stratifier(strata: list[str]) -> Callable[[dict[str, Any]], tuple]:
    def key(item: dict[str, Any]) -> tuple:
        return tuple(str(item.get(s, "unknown")) for s in strata)
    return key


def draw(matches: list[Match], orders: dict[str, tuple[int, int]],
         audit_config: dict, run: str) -> dict[str, Any]:
    strata = list(audit_config.get("strata", []))
    size = int(audit_config.get("confident_term_sample_size", 0))
    pop = population(matches, orders)
    result = stratified_sample(pop, stratifier(strata), size, strata,
                               seed_material=f"{POPULATION_NAME}|{run}")
    return {
        "population": POPULATION_NAME,
        "config": {"strata": strata, "confident_term_sample_size": size},
        "sample": result.as_dict(),
        "items": [pop[i] for i in result.indices],
        "note": ("drawn by stage 4 with the same algorithm and seed material as "
                 "pipeline/eval/sections/stratified_audit.py, so stage 8's own draw "
                 "over the same population is the same sample"),
    }
