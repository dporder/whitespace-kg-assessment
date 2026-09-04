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
from typing import Any, Optional

import config
from pipeline.eval.context import (CONCEPT_DUPLICATE_RATIO, CONCEPT_PAIR_SCAN_CAP,
                                   CONCEPT_TERM_SCAN_CAP, Context, LIST_CAP)
from pipeline.eval.rates import MEASURED, NO_DATA, PARTIAL, Rate, Section, cap
from pipeline.eval.sampling import stratified_sample
from pipeline.eval.text import normalise, similarity

SPOT_CHECK_SIZE = 10          # spec-silent; small on purpose, it is human time


def scan_units(ctx: Context, parts: Optional[list[str]] = None) -> list[tuple[str, str]]:
    """(part, path) of every unit SPEC 2.4 says concepts are extracted over."""
    units: list[tuple[str, str]] = []
    for part, tree in sorted(ctx.inputs.trees.items()):
        if parts is not None and part not in parts:
            continue
        units.append((part, tree.path))
        for child in tree.children:
            if child.kind != "ref":
                units.append((part, child.path))
    return units


def concept_scope(ctx: Context) -> dict[str, Any]:
    """How stage 5 scoped this run, and which loaded parts it covers.

    Absent scope file means stage 5 scanned everything it was given, which is
    the behaviour this section always had. Present, it names the parts scanned
    and the parts deliberately skipped, and the two must not be added together:
    a part with no concepts because nobody looked at it is a different fact from
    a part the scan looked at and found nothing in, and averaging them produces
    a coverage number that is wrong in the flattering direction.
    """
    loaded = sorted(ctx.inputs.trees)
    raw = ctx.inputs.concept_scope
    if not isinstance(raw, dict):
        return {"sampled": False, "in_scope": loaded, "skipped": [],
                "unmentioned": [], "declared_scanned": None, "declared_skipped": None,
                "scanned_but_not_loaded": []}

    def names(key: str) -> list[str]:
        value = raw.get(key)
        return sorted(str(x) for x in value) if isinstance(value, list) else []

    scanned, skipped = names("scanned_parts"), names("skipped_parts")
    in_scope = [p for p in loaded if p in scanned]
    return {
        "sampled": True,
        "source": "concepts/scope.json",
        "declared_scanned": scanned,
        "declared_skipped": skipped,
        "in_scope": in_scope,
        "skipped": [p for p in loaded if p in skipped],
        # Loaded, but the scope file mentions it in neither list. Left out of
        # the denominator, because nothing says it was scanned, and named
        # rather than quietly bucketed with the skips.
        "unmentioned": [p for p in loaded if p not in scanned and p not in skipped],
        # Declared scanned but this run has no tree for it, so its units cannot
        # be counted either way.
        "scanned_but_not_loaded": [p for p in scanned if p not in loaded],
    }


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
    # Duplicates are counted by cluster membership, not by pair. Four mutually
    # similar labels are six pairs but three redundant concepts, and counting
    # pairs into a member total produced rates above 1.0 (6/4). Union-find over
    # the similarity relation gives the number that can be deduplicated away.
    labels = [c.label for c in concepts]
    normalised = [normalise(c) for c in labels]
    exact = [{"label": lbl, "count": n}
             for lbl, n in Counter(normalised).items() if n > 1]

    parent = list(range(len(concepts)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = root(i), root(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    near: list[dict[str, Any]] = []
    pairs_examined = 0
    capped = len(concepts) > CONCEPT_PAIR_SCAN_CAP
    scanned = concepts[:CONCEPT_PAIR_SCAN_CAP] if capped else concepts
    for i in range(len(scanned)):
        for j in range(i + 1, len(scanned)):
            pairs_examined += 1
            if normalised[i] == normalised[j]:
                union(i, j)
                continue
            score = similarity(labels[i], labels[j])
            if score >= CONCEPT_DUPLICATE_RATIO:
                union(i, j)
                near.append({"a": labels[i], "b": labels[j], "similarity": score})

    clusters: Counter[int] = Counter(root(i) for i in range(len(concepts)))
    duplicate_members = sum(size - 1 for size in clusters.values())
    duplicate_rate = Rate(duplicate_members, len(concepts))

    # -- coverage ----------------------------------------------------------------
    scope = concept_scope(ctx)
    units = scan_units(ctx, scope["in_scope"] if scope["sampled"] else None)
    out_of_scope_units = (len(scan_units(ctx, scope["skipped"] + scope["unmentioned"]))
                          if scope["sampled"] else 0)
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
    elif scope["sampled"]:
        coverage = Rate(0, 0)
        uncovered = []
        coverage_note = ("no loaded part is in this run's concept scope, so there is "
                         "nothing to measure coverage over")
    else:
        coverage = Rate(0, 0)
        uncovered = []
        coverage_note = "no stage 2 trees loaded, so there are no scan units to cover"

    # -- concept labels colliding with declared terms ---------------------------
    collisions: list[dict[str, Any]] = []
    collision_capped = False
    if ctx.inputs.definition_sites is not None:
        terms = {d.term for d in ctx.inputs.definition_sites}
        aliases = {a for d in ctx.inputs.definition_sites for a in d.aliases}
        vocabulary = sorted(terms | aliases)
        collision_capped = (len(concepts) > CONCEPT_TERM_SCAN_CAP
                            or len(vocabulary) > CONCEPT_TERM_SCAN_CAP)
        for c in concepts[:CONCEPT_TERM_SCAN_CAP]:
            for t in vocabulary[:CONCEPT_TERM_SCAN_CAP]:
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
        "duplicate_clusters": len([n for n in clusters.values() if n > 1]),
        "pair_scan": {
            "pairs_examined": pairs_examined,
            "capped": capped,
            "cap": CONCEPT_PAIR_SCAN_CAP,
            "note": (f"only the first {CONCEPT_PAIR_SCAN_CAP} concepts were compared "
                     f"all-pairs; duplicates beyond that are not counted"
                     if capped else None),
        },
        "collision_scan_capped": collision_capped,
        "coverage": coverage.as_dict(),
        "coverage_note": coverage_note,
        "coverage_is_over": ("the parts stage 5 scanned this run" if scope["sampled"]
                             else "every loaded part; stage 5 declared no sampling scope"),
        "concept_scope": scope,
        "parts_outside_this_runs_concept_scope": len(scope["skipped"]),
        "scan_units_outside_this_runs_concept_scope": out_of_scope_units,
        "scan_units_with_no_concept": cap(uncovered, LIST_CAP)[0],
        "scan_units_with_no_concept_not_listed": cap(uncovered, LIST_CAP)[1],
        "concept_label_collides_with_a_declared_term": collisions,
        "member_node_ids_not_in_any_loaded_tree": len(orphan_members),
        "spot_check": {"sample": sample.as_dict(), "items": spot},
    })

    s.line(f"**{len(concepts)}** concept(s) in scope.")
    s.line()
    rows = [["duplicate rate after resolution (lexical proxy)", str(duplicate_rate)],
            ["coverage: in-scope scan units with at least one concept", str(coverage)]]
    if scope["sampled"]:
        rows.append(["parts outside this run's concept scope",
                     f"{len(scope['skipped'])} "
                     f"({out_of_scope_units} scan unit(s), not counted as misses)"])
    rows += [["concept labels colliding with a declared term", len(collisions)],
             ["member node ids not in any loaded tree", len(orphan_members)]]
    s.table(["measure", "value"], rows)
    if scope["sampled"]:
        s.bullet(f"stage 5 sampled this run (`{scope['source']}`): coverage is over "
                 f"{len(scope['in_scope'])} scanned part(s), "
                 f"{', '.join(scope['in_scope']) or 'none'}. A part nobody looked at is "
                 f"not a part the scan missed, so the skipped ones are counted "
                 f"separately and never folded into the miss rate.")
        if scope["skipped"]:
            s.bullet(f"skipped by stage 5: {', '.join(scope['skipped'])}")
        if scope["unmentioned"]:
            s.bullet(f"loaded but named in neither list in {scope['source']}, so left "
                     f"out of the denominator: {', '.join(scope['unmentioned'])}")
        if scope["scanned_but_not_loaded"]:
            s.bullet(f"declared scanned but no tree loaded here, so not counted either "
                     f"way: {', '.join(scope['scanned_but_not_loaded'])}")
    s.bullet(s.data["duplicate_method"])
    s.bullet("duplicates are counted as cluster membership (a cluster of n costs "
             "n-1), not as similar pairs, so the rate cannot exceed 1")
    if s.data["pair_scan"]["note"]:
        s.bullet(s.data["pair_scan"]["note"])
    if collision_capped:
        s.bullet(f"the concept-to-term collision scan was capped at "
                 f"{CONCEPT_TERM_SCAN_CAP} on each side; collisions beyond that are "
                 f"not counted")
    if coverage_note:
        s.bullet(coverage_note)
    if uncovered:
        s.line()
        s.line(f"**{len(uncovered)}** in-scope scan unit(s) received no concept "
               f"(the scan looked and found nothing, as distinct from the skipped "
               f"parts above):")
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
