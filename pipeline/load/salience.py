"""Salience: breadth times log-damped frequency, computed at load time.

DESIGN 3: "For a provision, salience is the number of distinct parts whose refs
resolve to it, multiplied by the log of one plus the resolved citation count.
For a term, the same with parts that use it and use count... Repeated furniture,
headers, footers, form placeholders, is excluded from the counts entirely, and
anything whose frequency is wildly out of distribution for its kind gets flagged
for eyes rather than boosted."

The shape matters more than the constants: breadth linear, frequency log damped,
because the most repeated string in a corpus is usually boilerplate while being
needed from many distinct places is real evidence of load bearing. It is a
ranking aid for retrieval and review, never a statement of legal weight, and
because it is a pure function of the graph it recomputes on every load.

Furniture exclusion needs a definition the code can apply. A citing node counts
as furniture when its normalised own text repeats across at least
`furniture_min_repeats` nodes spanning at least `furniture_min_parts` parts, or
when it is a form placeholder. Both thresholds are read from `config.SALIENCE`
when the orchestrator adds them, with the documented defaults below until then.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

import config
from pipeline.schemas import Node, TermUse, normalise_for_hash

SALIENCE_DEFAULTS = {
    "furniture_min_repeats": 3,     # identical own text on this many nodes...
    "furniture_min_parts": 2,       # ...spanning this many parts is furniture
    "outlier_sigma": 4.0,           # frequency this far above its kind's mean is flagged
}
PLACEHOLDER = re.compile(r"^\s*\[[^\]]{0,80}\]\s*$")


def settings() -> dict:
    out = dict(SALIENCE_DEFAULTS)
    out.update({k: v for k, v in (getattr(config, "SALIENCE", {}) or {}).items()
                if k in SALIENCE_DEFAULTS})
    return out


@dataclass
class Salience:
    values: dict[str, float] = field(default_factory=dict)          # node id -> salience
    term_values: dict[str, float] = field(default_factory=dict)     # term name -> salience
    furniture: set[str] = field(default_factory=set)                # node ids excluded
    flagged: dict[str, str] = field(default_factory=dict)           # id -> why
    report: dict = field(default_factory=dict)


def furniture_nodes(nodes: list[Node], cfg: dict) -> set[str]:
    """Repeated furniture and form placeholders, excluded from every count."""
    by_text: dict[str, list[Node]] = {}
    placeholders: set[str] = set()
    for node in nodes:
        if not node.text:
            continue
        if PLACEHOLDER.match(node.text):
            placeholders.add(node.id)
            continue
        by_text.setdefault(normalise_for_hash(node.text), []).append(node)
    out = set(placeholders)
    for group in by_text.values():
        parts = {n.path.split("/", 1)[0] for n in group}
        if (len(group) >= int(cfg["furniture_min_repeats"])
                and len(parts) >= int(cfg["furniture_min_parts"])):
            out.update(n.id for n in group)
    return out


def _score(breadth: int, frequency: int) -> float:
    return round(breadth * math.log(1 + frequency), 6)


def _flag_outliers(frequency: dict[str, int], kind_of: dict[str, str],
                   sigma: float) -> dict[str, str]:
    by_kind: dict[str, list[int]] = {}
    for key, count in frequency.items():
        by_kind.setdefault(kind_of.get(key, "?"), []).append(count)
    flagged: dict[str, str] = {}
    for kind, counts in by_kind.items():
        if len(counts) < 3:
            continue
        mean = sum(counts) / len(counts)
        variance = sum((c - mean) ** 2 for c in counts) / len(counts)
        stdev = math.sqrt(variance)
        if stdev == 0:
            continue
        limit = mean + sigma * stdev
        for key, count in frequency.items():
            if kind_of.get(key, "?") == kind and count > limit:
                flagged[key] = (f"frequency {count} is more than {sigma} standard "
                                f"deviations above the mean {mean:.2f} for kind {kind}")
    return flagged


def compute(nodes: list[Node], refs: list[Node], uses: list[TermUse],
            cfg: Optional[dict] = None) -> Salience:
    """Salience for every structural node and every term."""
    cfg = cfg or settings()
    out = Salience()
    by_id = {n.id: n for n in nodes}
    by_path = {n.path: n for n in nodes}
    out.furniture = furniture_nodes(nodes, cfg)

    citing_part = {}
    for ref in refs:
        citing_part[ref.path] = ref.path.split("/", 1)[0]

    breadth: dict[str, set[str]] = {}
    frequency: dict[str, int] = {}
    excluded = 0
    for ref in refs:
        if ref.status != "resolved" or not ref.target_path:
            continue
        target = by_path.get(ref.target_path)
        if target is None:
            continue
        parent_path = ref.path.rsplit("/ref@", 1)[0]
        parent = by_path.get(parent_path)
        if parent is not None and parent.id in out.furniture:
            excluded += 1
            continue
        breadth.setdefault(target.id, set()).add(citing_part[ref.path])
        frequency[target.id] = frequency.get(target.id, 0) + 1

    for node in nodes:
        out.values[node.id] = _score(len(breadth.get(node.id, ())),
                                     frequency.get(node.id, 0))

    term_breadth: dict[str, set[str]] = {}
    term_frequency: dict[str, int] = {}
    term_excluded = 0
    for use in uses:
        node = by_id.get(use.node_id)
        if node is None:
            continue
        if node.id in out.furniture:
            term_excluded += 1
            continue
        term_breadth.setdefault(use.term, set()).add(node.path.split("/", 1)[0])
        term_frequency[use.term] = term_frequency.get(use.term, 0) + 1
    for term in term_frequency:
        out.term_values[term] = _score(len(term_breadth[term]), term_frequency[term])

    sigma = float(cfg["outlier_sigma"])
    out.flagged = _flag_outliers(frequency, {n.id: n.kind for n in nodes}, sigma)
    out.flagged.update(_flag_outliers(term_frequency,
                                      {t: "term" for t in term_frequency}, sigma))
    out.report = {
        "settings": cfg,
        "config_keys_requested": ["SALIENCE.furniture_min_repeats",
                                  "SALIENCE.furniture_min_parts",
                                  "SALIENCE.outlier_sigma"],
        "formula": "salience = breadth * log(1 + frequency)",
        "nodes_scored": len(out.values),
        "nodes_with_salience": sum(1 for v in out.values.values() if v > 0),
        "terms_scored": len(out.term_values),
        "furniture_nodes_excluded": len(out.furniture),
        "citations_excluded_as_furniture": excluded,
        "term_uses_excluded_as_furniture": term_excluded,
        "flagged_out_of_distribution": len(out.flagged),
        "top_nodes": [{"path": by_id[i].path, "salience": v}
                      for i, v in sorted(out.values.items(),
                                         key=lambda kv: (-kv[1], kv[0]))[:10] if v > 0],
        "top_terms": [{"term": t, "salience": v}
                      for t, v in sorted(out.term_values.items(),
                                         key=lambda kv: (-kv[1], kv[0]))[:10] if v > 0],
    }
    return out
