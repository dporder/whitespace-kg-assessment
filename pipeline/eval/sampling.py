"""Deterministic stratified sampling.

EVALUATION.md layer 4: a random sample of confident matches, "stratified by
term length, part and position so it mirrors the population distribution".
Proportional allocation with largest remainder, deterministic tie-breaks, and a
seed derived from the population itself, so the same population always yields
the same sample and a changed population yields a different but reproducible
one. No wall clock, no global RNG.

Scale independence is the point: the same code draws 40 from 400 and 40 from
400,000, and when the population is smaller than the requested size it takes
everything and says so.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence, TypeVar

T = TypeVar("T")


@dataclass
class Stratum:
    key: tuple
    population: int
    allocated: int
    taken: list[int] = field(default_factory=list)     # indices into the population

    def as_dict(self) -> dict[str, Any]:
        return {"stratum": " | ".join(str(k) for k in self.key),
                "population": self.population, "sampled": len(self.taken)}


@dataclass
class SampleResult:
    population_size: int
    requested: int
    indices: list[int] = field(default_factory=list)   # sampled indices, ascending
    strata: list[Stratum] = field(default_factory=list)
    strata_names: list[str] = field(default_factory=list)
    seed: str = ""

    @property
    def size(self) -> int:
        return len(self.indices)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strata": self.strata_names,
            "population_size": self.population_size,
            "requested_sample_size": self.requested,
            "drawn_sample_size": self.size,
            "cells": [s.as_dict() for s in self.strata],
            "seed": self.seed,
        }


def _seed_for(material: str) -> int:
    return int(hashlib.sha1(material.encode()).hexdigest()[:16], 16)


def stratified_sample(population: Sequence[T],
                      stratifier: Callable[[T], tuple],
                      size: int,
                      strata_names: Iterable[str],
                      seed_material: str = "") -> SampleResult:
    """Proportional stratified sample of `size` items, deterministic.

    Allocation is by largest remainder over the cell populations, ties broken
    by cell key so two runs cannot disagree. Within a cell the draw is a
    seeded shuffle. Returns indices into `population`, ascending.
    """
    n = len(population)
    result = SampleResult(population_size=n, requested=size,
                          strata_names=list(strata_names))
    if n == 0 or size <= 0:
        return result

    cells: dict[tuple, list[int]] = {}
    for i, item in enumerate(population):
        cells.setdefault(tuple(stratifier(item)), []).append(i)
    keys = sorted(cells, key=lambda k: tuple(str(x) for x in k))

    material = seed_material + "|" + "|".join(
        f"{k}:{len(cells[k])}" for k in keys)
    result.seed = hashlib.sha1(material.encode()).hexdigest()[:16]

    if size >= n:                                   # take everything, say so
        for k in keys:
            result.strata.append(Stratum(key=k, population=len(cells[k]),
                                         allocated=len(cells[k]),
                                         taken=sorted(cells[k])))
        result.indices = sorted(range(n))
        return result

    exact = {k: size * len(cells[k]) / n for k in keys}
    base = {k: int(exact[k]) for k in keys}
    remaining = size - sum(base.values())
    # Largest remainder, ties by cell key: deterministic under any input order.
    order = sorted(keys, key=lambda k: (-(exact[k] - base[k]),
                                        tuple(str(x) for x in k)))
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


def position_bucket(order: int, total: int) -> str:
    """Position within a part as a third: early, middle or late."""
    if total <= 1:
        return "only"
    frac = order / max(total - 1, 1)
    if frac < 1 / 3:
        return "early"
    if frac < 2 / 3:
        return "middle"
    return "late"


def word_count_bucket(text: str) -> str:
    words = len(text.split())
    if words <= 1:
        return "1 word"
    if words == 2:
        return "2 words"
    return "3+ words"
