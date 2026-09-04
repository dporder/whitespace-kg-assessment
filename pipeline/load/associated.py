"""ASSOCIATED_TERM: the join that has to happen here and nowhere else.

SPEC 2.4: "After resolution, compute `ASSOCIATED_TERM` edges, concept to term,
deterministically, for each concept the terms its member provisions use,
weighted by the share of member provisions using the term, kept above
`ASSOCIATED_TERM_MIN_SHARE` in `config.py`. This aggregation joins stage 4 and
stage 5 outputs, and stages 3 to 6 never read each other's output, so it runs
inside stage 7, the join."

The arithmetic is deterministic; its inputs include generated tags, so the edge
carries `llm_derived: true` and stays navigation, never a citation path.

The share's denominator is the concept's member provisions that this run can
see. A member the graph does not hold cannot be counted as using or not using a
term, so it is excluded from both halves and the count of excluded members is
reported rather than quietly changing the denominator.
"""
from __future__ import annotations

from typing import Optional

import config
from pipeline.schemas import Concept, GraphEdge, Node, TermUse

from .rows import Rows, edge


def min_share() -> float:
    return float(getattr(config, "ASSOCIATED_TERM_MIN_SHARE", 0.25))


def build(concepts: list[Concept], uses: list[TermUse], nodes_by_id: dict[str, Node],
          *, batch_id: str, threshold: Optional[float] = None) -> Rows:
    share_min = min_share() if threshold is None else threshold
    rows = Rows()
    terms_by_node: dict[str, set[str]] = {}
    for use in uses:
        terms_by_node.setdefault(use.node_id, set()).add(use.term)

    for concept in concepts:
        members = [m for m in concept.member_node_ids if m in nodes_by_id]
        missing = len(concept.member_node_ids) - len(members)
        if missing:
            rows.notes.append({"kind": "associated_term_members_missing",
                               "concept": concept.id, "missing": missing,
                               "counted": len(members),
                               "detail": "members this run does not hold are excluded "
                                         "from both halves of the share"})
        if not members:
            continue
        counts: dict[str, int] = {}
        for node_id in members:
            for term in terms_by_node.get(node_id, ()):
                counts[term] = counts.get(term, 0) + 1
        for term in sorted(counts):
            share = round(counts[term] / len(members), 4)
            if share < share_min:
                continue
            rows.edges.append(edge("ASSOCIATED_TERM", concept.id, term, batch_id,
                                   share=share, llm_derived=True,
                                   members_using=counts[term],
                                   members_counted=len(members)))
    return rows


def summary(edges: list[GraphEdge]) -> dict:
    associated = [e for e in edges if e.type == "ASSOCIATED_TERM"]
    return {"edges": len(associated),
            "min_share": min_share(),
            "concepts_with_terms": len({e.src for e in associated}),
            "note": "deterministic aggregation over mixed-trust inputs; flagged "
                    "llm_derived and never part of a citation path"}
