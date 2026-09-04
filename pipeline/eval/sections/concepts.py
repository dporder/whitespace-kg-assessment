"""`concepts`: duplicate rate after resolution, coverage, spot-check sample.

SPEC 2.6 and EVALUATION.md layer 6. Concepts are the generative tier. They are
navigation, not citation, so the bar is usefulness and hygiene rather than
fidelity, and the metrics say so rather than pretending one measure fits all
three tiers.

Duplicates. SPEC 2.4 resolves near duplicates by embedding cosine at
`config.CONCEPT_MERGE_COSINE`. That needs stage 6 vectors, so what runs here is
a **lexical proxy**: exact normalised-label collisions plus high string
similarity. It is labelled a proxy everywhere it appears, because a lexical
check that calls itself a cosine check is a lie about what was measured.

Coverage. A scan unit that received no concept at all is the interesting cell:
SPEC 2.4 scans a part or top-level clause, so those are the units counted.

A concept whose label collides with a declared Term should never have been
minted (SPEC 2.4, tier 2 outranks tier 3). Collisions are counted here.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

import config
from pipeline.eval.context import CONCEPT_DUPLICATE_RATIO, Context, LIST_CAP
from pipeline.eval.rates import MEASURED, NO_DATA, PARTIAL, Rate, Section, cap
from pipeline.eval.sampling import stratified_sample
from pipeline.eval.text import normalise, similarity

SPOT_CHECK_SIZE = 10          # spec-silent; small on purpose, it is human time


def scan_units(ctx: Context) -> list[tuple[str, str]]:
    """(part, path) of every unit SPEC 2.4 says concepts are extracted over."""
    units: list[tuple[str, str]] = []
    for part, tree in sorted(ctx.inputs.trees.items()):
        units.append((part, tree.path))
        for child in tree.children:
            if child.kind != "ref":
                units.append((part, child.path))
    return units


def build(ctx: Context) -> Section:
    s = Section("concepts")
    concepts = ctx.inputs.concepts
    if concepts is None:
        s.status = NO_DATA
        s.reason = f"stage 5 output absent; looked for {ctx.inputs.root}/concepts.json"
        s.line(f"_{s.reason}_")
        return s
    if not concepts:
        s.status = NO_DATA
        s.reason = "stage 5 output is present but empty"
        s.line(f"_{s.reason}_")
        return s

    # -- duplicates, lexical proxy for the cosine check --------------------------
    labels = [c.label for c in concepts]
    normalised = [normalise(c) for c in labels]
    exact = [{"label": lbl, "count": n}
             for lbl, n in Counter(normalised).items() if n > 1]
    near: list[dict[str, Any]] = []
    for i in range(len(concepts)):
        for j in range(i + 1, len(concepts)):
            if normalised[i] == normalised[j]:
                continue
            score = similarity(labels[i], labels[j])
            if score >= CONCEPT_DUPLICATE_RATIO:
                near.append({"a": labels[i], "b": labels[j], "similarity": score})
    duplicate_members = sum(d["count"] - 1 for d in exact) + len(near)
    duplicate_rate = Rate(duplicate_members, len(concepts))

    # -- coverage ----------------------------------------------------------------
    units = scan_units(ctx)
    if units:
        member_nodes = {nid for c in concepts for nid in c.member_node_ids}
        by_id = ctx.inputs.nodes_by_id()
        paths_with_members = {by_id[nid].path for nid in member_nodes if nid in by_id}
        scoped = {c.scope_path for c in concepts}
        covered = []
        uncovered = []
        for part, path in units:
            hit = path in scoped or any(p == path or p.startswith(path + "/")
                                        for p in paths_with_members)
            (covered if hit else uncovered).append({"part": part, "path": path})
        coverage = Rate(len(covered), len(units))
        coverage_note = None
    else:
        coverage = Rate(0, 0)
        uncovered = []
        coverage_note = "no stage 2 trees loaded, so there are no scan units to cover"

    # -- concept labels colliding with declared terms ---------------------------
    collisions: list[dict[str, Any]] = []
    if ctx.inputs.definition_sites is not None:
        terms = {d.term for d in ctx.inputs.definition_sites}
        aliases = {a for d in ctx.inputs.definition_sites for a in d.aliases}
        for c in concepts:
            for t in sorted(terms | aliases):
                if normalise(c.label) == normalise(t):
                    collisions.append({"concept": c.label, "term": t, "match": "exact"})
                elif similarity(c.label, t) >= CONCEPT_DUPLICATE_RATIO:
                    collisions.append({"concept": c.label, "term": t,
                                       "match": f"lexical {similarity(c.label, t)}"})

    # -- orphan members ----------------------------------------------------------
    by_id = ctx.inputs.nodes_by_id()
    orphan_members = [nid for c in concepts for nid in c.member_node_ids
                      if by_id and nid not in by_id]

    # -- spot check --------------------------------------------------------------
    sample = stratified_sample(concepts, lambda c: (c.scope_path.split("/")[0],),
                               SPOT_CHECK_SIZE, ["part"],
                               seed_material=f"concept-spot-check|{ctx.run}")
    spot = [{"id": concepts[i].id, "label": concepts[i].label,
             "scope_path": concepts[i].scope_path,
             "members": len(concepts[i].member_node_ids),
             "confidence": concepts[i].confidence,
             "member_paths": [by_id[n].path for n in concepts[i].member_node_ids
                              if n in by_id][:5]}
            for i in sample.indices]

    s.status = MEASURED if ctx.inputs.trees else PARTIAL
    if not ctx.inputs.trees:
        s.reason = "no stage 2 trees loaded; coverage and member checks not run"
    s.data.update({
        "concepts_total": len(concepts),
        "duplicate_rate_after_resolution": duplicate_rate.as_dict(),
        "duplicate_method": (f"lexical proxy: exact normalised-label collision, or string "
                             f"similarity >= {CONCEPT_DUPLICATE_RATIO}. The specified check "
                             f"is embedding cosine >= {config.CONCEPT_MERGE_COSINE} "
                             f"(config.CONCEPT_MERGE_COSINE), which needs stage 6 vectors."),
        "exact_label_collisions": exact,
        "near_duplicate_pairs": cap(near, LIST_CAP)[0],
        "coverage": coverage.as_dict(),
        "coverage_note": coverage_note,
        "scan_units_with_no_concept": cap(uncovered, LIST_CAP)[0],
        "scan_units_with_no_concept_not_listed": cap(uncovered, LIST_CAP)[1],
        "concept_label_collides_with_a_declared_term": collisions,
        "member_node_ids_not_in_any_loaded_tree": len(orphan_members),
        "spot_check": {"sample": sample.as_dict(), "items": spot},
    })

    s.line(f"**{len(concepts)}** concept(s) in scope.")
    s.line()
    s.table(["measure", "value"],
            [["duplicate rate after resolution (lexical proxy)", str(duplicate_rate)],
             ["coverage: scan units with at least one concept", str(coverage)],
             ["concept labels colliding with a declared term", len(collisions)],
             ["member node ids not in any loaded tree", len(orphan_members)]])
    s.bullet(s.data["duplicate_method"])
    if coverage_note:
        s.bullet(coverage_note)
    if uncovered:
        s.line()
        s.line(f"**{len(uncovered)}** scan unit(s) received no concept:")
        s.table(["part", "path"], [[u["part"], u["path"]]
                                   for u in cap(uncovered, LIST_CAP)[0]])
    if collisions:
        s.line()
        s.table(["concept label", "collides with term", "match"],
                [[c["concept"], c["term"], c["match"]] for c in collisions[:LIST_CAP]])
    s.line()
    s.line(f"**Spot check for human eyes**, {len(spot)} of {len(concepts)} "
           f"(seed `{sample.seed}`):")
    s.table(["label", "scope", "members", "confidence", "example member paths"],
            [[c["label"], c["scope_path"], c["members"], c["confidence"],
              ", ".join(c["member_paths"])] for c in spot])
    return s
