"""`definitions_vs_provided`: the discovered vocabulary against the declared list.

SPEC 2.6: "discovery precision and recall against the declared JS1 list, terms
discovered outside JS1, capitalised phrases used but never defined."

The declared list is Joint Schedule 1's own definitions schedule, as recorded by
stage 4 in `DefinitionSite.source` (`declared`, `discovered` or `both`). SPEC 2.3
requires those lists kept apart precisely so this diff is possible, so the
provided artifact here is the document's own schedule rather than a third-party
file.

A term discovered outside the schedule is **not** automatically a false
positive: SPEC 2.3 allows part-local definitions that shadow JS1. It lowers
precision against the declared list and is also listed on its own, which is the
interesting remainder EVALUATION.md layer 2 asks for.

Also reported here, because they are vocabulary hygiene and nothing else owns
them: the `DEFINED_USING` dependency graph's cycles and maximum chain depth
(SPEC 2.3), and term uses whose term has no definition site at all.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from pipeline.eval.context import Context, CYCLE_SCC_CAP, LIST_CAP
from pipeline.eval.rates import MEASURED, NO_DATA, PARTIAL, Rate, Section, cap
from pipeline.eval.text import title_case_runs

# Words that open a sentence and are capitalised for that reason alone.
_SENTENCE_END = re.compile(r"[.;:!?]\s*$")


def _sentence_initial(text: str, start: int) -> bool:
    before = text[:start].rstrip()
    return not before or bool(_SENTENCE_END.search(before))


def capitalised_never_defined(ctx: Context, known: set[str]) -> dict[str, Any]:
    """Capitalised phrases that appear in the text but match no defined term.

    Deliberately a candidate count, not a term list. Title case is weak
    evidence, and this document's typo-dense sections make it weaker in both
    directions (SPEC 2.3), so sentence-initial single words are counted apart
    from multi-word runs rather than mixed into one number.
    """
    if not ctx.inputs.trees:
        return {"status": "no_data", "reason": "no stage 2 trees loaded"}
    multi: Counter[str] = Counter()
    single_initial: Counter[str] = Counter()
    single_mid: Counter[str] = Counter()
    for _part, node in ctx.inputs.nodes():
        for field in (node.text, node.title):
            if not field:
                continue
            for start, _end, surface in title_case_runs(field):
                if surface in known:
                    continue
                if any(surface in k or k in surface for k in known):
                    continue
                if " " in surface:
                    multi[surface] += 1
                elif _sentence_initial(field, start):
                    single_initial[surface] += 1
                else:
                    single_mid[surface] += 1
    return {
        "status": "measured",
        "multi_word_phrases": {"distinct": len(multi), "occurrences": sum(multi.values()),
                               "top": [{"phrase": p, "occurrences": c}
                                       for p, c in multi.most_common(LIST_CAP)]},
        "single_word_mid_sentence": {"distinct": len(single_mid),
                                     "occurrences": sum(single_mid.values()),
                                     "top": [{"phrase": p, "occurrences": c}
                                             for p, c in single_mid.most_common(LIST_CAP)]},
        "single_word_sentence_initial": {"distinct": len(single_initial),
                                         "occurrences": sum(single_initial.values()),
                                         "note": "capitalised because a sentence starts "
                                                 "there; weak evidence either way"},
    }


def defined_using_graph(ctx: Context) -> dict[str, Any]:
    """Term-to-term edges from term uses inside definition texts (SPEC 2.3)."""
    if ctx.inputs.definition_sites is None or ctx.inputs.term_uses is None:
        return {"status": "no_data",
                "reason": "stage 4 vocabulary output absent (definition_sites/term_uses)"}
    term_of_definition_node: dict[str, str] = {
        d.definition_node_id: d.term for d in ctx.inputs.definition_sites}
    edges = sorted({(term_of_definition_node[u.node_id], u.term)
                    for u in ctx.inputs.term_uses
                    if u.node_id in term_of_definition_node
                    and term_of_definition_node[u.node_id] != u.term})
    if not edges:
        return {"status": "measured", "edges": 0, "cycles": [], "max_chain_depth": 0,
                "note": "no term use falls inside another term's definition text"}
    try:
        import networkx as nx
    except Exception as exc:                              # noqa: BLE001
        return {"status": "partial", "edges": len(edges),
                "reason": f"networkx unavailable, cycles not computed: {exc}"}
    g = nx.DiGraph()
    for src, dst in edges:
        g.add_edge(src, dst)

    # Cycle enumeration is exponential in the worst case, and the real Joint
    # Schedule 1 defines hundreds of terms in terms of each other, so `--full`
    # could sit here indefinitely. Cycles are enumerated only inside strongly
    # connected components small enough to be safe; a larger one is reported as
    # what it is, a tangle of known size, rather than silently omitted or
    # silently hung on.
    cycles: list[list[str]] = []
    unenumerated: list[dict[str, Any]] = []
    for component in nx.strongly_connected_components(g):
        if len(component) < 2:
            continue
        if len(component) > CYCLE_SCC_CAP:
            members = sorted(component)
            unenumerated.append({
                "scc_size": len(component),
                "member_sample": members[:LIST_CAP],
                "reason": f"cycles not enumerated: strongly connected component of "
                          f"{len(component)} terms exceeds the cap of {CYCLE_SCC_CAP}; "
                          f"every term in it is on some cycle",
            })
            continue
        sub = g.subgraph(component)
        cycles.extend(sorted(c) for c in nx.simple_cycles(sub))
    cycles.sort()

    has_cycle = bool(cycles or unenumerated)
    if has_cycle:
        cond = nx.condensation(g)
        depth = nx.dag_longest_path_length(cond) + 1
        note = "chain depth measured over the condensation, cycles collapsed"
    else:
        depth = nx.dag_longest_path_length(g) + 1
        note = None
    return {"status": "measured", "edges": len(edges), "edge_list": edges[:LIST_CAP],
            "cycles": cycles, "cycles_not_enumerated": unenumerated,
            "scc_enumeration_cap": CYCLE_SCC_CAP,
            "max_chain_depth": depth, "note": note}


def build(ctx: Context) -> Section:
    s = Section("definitions_vs_provided")
    sites = ctx.inputs.definition_sites
    if sites is None:
        s.status = NO_DATA
        s.reason = ("stage 4 vocabulary output absent; looked for "
                    f"{ctx.inputs.root}/vocab/definition_sites.json")
        s.data["defined_using_graph"] = defined_using_graph(ctx)
        s.line(f"_{s.reason}_")
        return s

    declared = sorted({d.term for d in sites if d.source in ("declared", "both")})
    discovered = sorted({d.term for d in sites if d.source in ("discovered", "both")})
    both = sorted(set(declared) & set(discovered))
    discovered_only = sorted(set(discovered) - set(declared))
    declared_only = sorted(set(declared) - set(discovered))

    precision = Rate(len(both), len(discovered))
    recall = Rate(len(both), len(declared))

    known: set[str] = set()
    for d in sites:
        known.add(d.term)
        known.update(d.aliases)

    uses_without_site: list[dict[str, Any]] = []
    if ctx.inputs.term_uses is not None:
        site_terms = {d.term for d in sites}
        counts = Counter(u.term for u in ctx.inputs.term_uses if u.term not in site_terms)
        uses_without_site = [{"term": t, "uses": c} for t, c in counts.most_common()]

    s.status = MEASURED if ctx.inputs.term_uses is not None else PARTIAL
    if ctx.inputs.term_uses is None:
        s.reason = "stage 4 term_uses absent; use-side checks not run"
    s.data.update({
        "declared_terms": len(declared),
        "discovered_terms": len(discovered),
        "discovered_and_declared": len(both),
        "discovery_precision_against_declared": precision.as_dict(),
        "discovery_recall_against_declared": recall.as_dict(),
        "discovered_outside_the_declared_schedule": {
            "count": len(discovered_only),
            "terms": cap(discovered_only, LIST_CAP)[0],
            "not_listed": cap(discovered_only, LIST_CAP)[1],
            "note": "not necessarily false positives: SPEC 2.3 allows part-local "
                    "definitions that shadow the document-level schedule",
        },
        "declared_but_not_rediscovered": {
            "count": len(declared_only),
            "terms": cap(declared_only, LIST_CAP)[0],
            "not_listed": cap(declared_only, LIST_CAP)[1],
        },
        "term_uses_with_no_definition_site": uses_without_site[:LIST_CAP],
        "capitalised_but_never_defined": capitalised_never_defined(ctx, known),
        "defined_using_graph": defined_using_graph(ctx),
    })

    s.line(f"Declared (Joint Schedule 1) **{len(declared)}** terms, "
           f"discovered by the rule **{len(discovered)}**, in both **{len(both)}**.")
    s.line()
    s.table(["measure", "value"],
            [["discovery precision against the declared list", str(precision)],
             ["discovery recall against the declared list", str(recall)],
             ["discovered outside the declared schedule", len(discovered_only)],
             ["declared but not rediscovered", len(declared_only)]])
    if discovered_only:
        s.bullet("outside the schedule: " + ", ".join(discovered_only[:LIST_CAP]))
    if declared_only:
        s.bullet("declared but not rediscovered: " + ", ".join(declared_only[:LIST_CAP]))
    if uses_without_site:
        s.line()
        s.line("**Term uses whose term has no definition site** "
               "(a use pointing at nothing is a broken edge, not a metric):")
        s.table(["term", "uses"], [[u["term"], u["uses"]] for u in uses_without_site[:LIST_CAP]])
    cap_never = s.data["capitalised_but_never_defined"]
    s.line()
    if cap_never["status"] == "measured":
        m = cap_never["multi_word_phrases"]
        smid = cap_never["single_word_mid_sentence"]
        si = cap_never["single_word_sentence_initial"]
        s.line(f"**Capitalised but never defined**: {m['distinct']} distinct multi-word "
               f"phrase(s) in {m['occurrences']} occurrence(s); "
               f"{smid['distinct']} single word(s) mid-sentence; "
               f"{si['distinct']} single word(s) sentence-initial (weak evidence).")
        s.table(["phrase", "occurrences"],
                [[p["phrase"], p["occurrences"]] for p in m["top"] + smid["top"]])
    else:
        s.bullet(f"capitalised but never defined: {cap_never['reason']}")
    g = s.data["defined_using_graph"]
    s.line()
    if g["status"] == "measured":
        s.bullet(f"DEFINED_USING graph: {g['edges']} edge(s), "
                 f"{len(g['cycles'])} cycle(s), max chain depth {g['max_chain_depth']}")
        for c in g["cycles"][:LIST_CAP]:
            s.bullet(f"cycle: {' -> '.join(c)}")
        for u in g.get("cycles_not_enumerated", [])[:LIST_CAP]:
            s.bullet(f"{u['reason']}; members include "
                     f"{', '.join(u['member_sample'][:8])}")
    else:
        s.bullet(f"DEFINED_USING graph: {g['status']}, {g.get('reason') or g.get('note')}")
    return s
