"""`golden_terms`: term-use detection and precision, per ambiguity kind, cost weighted.

SPEC 2.6: "same shape for term uses, with the FP and FN counts reported per
ambiguity kind and a cost weighted summary, weights in config.py as placeholders
a domain expert would set."

The two errors are not symmetric (EVALUATION.md section 2). A false positive
pollutes the graph; a false negative hides an obligation from the person
searching for it, which in this domain is usually worse. The weights in
`config.ERROR_COSTS` say so numerically, and they are declared placeholders: the
report prints the weighted total *and* the raw per-cell counts so the judgement
stays visible and reversible.

For a false positive the ambiguity kind comes from the pipeline's own record.
For a false negative there is no pipeline record to read one off, so the harness
derives it deterministically (heading, sentence initial, else none) and labels
that column `eval_derived` rather than pretending stage 4 said it.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional

import config
from pipeline.eval.context import Context, LIST_CAP
from pipeline.eval.golden import TERM_KIND, GoldenRecord
from pipeline.eval.rates import MEASURED, NO_DATA, Rate, Section, cap
from pipeline.schemas import Node, TermUse

AMBIGUITY_KINDS = ("none", "sentence_initial", "heading", "typo_dense", "alias_collision")
_SENTENCE_END = re.compile(r"[.;:!?]\s*$")


def derive_ambiguity_kind(node: Optional[Node], span: tuple[int, int]) -> str:
    """The kind the harness can see from the text alone.

    Deliberately conservative: `typo_dense` is a per-section signal stage 4
    computes and is not derivable here, so a missed use in a typo-dense section
    lands in `none` and the report says the column is eval-derived.
    """
    if node is None:
        return "none"
    start = span[0]
    if node.text is not None and start < len(node.text):
        before = node.text[:start].rstrip()
        return "sentence_initial" if (not before or _SENTENCE_END.search(before)) else "none"
    if node.title:
        return "heading"
    return "none"


def use_index(uses: list[TermUse]) -> dict[tuple[str, int, int], list[TermUse]]:
    out: dict[tuple[str, int, int], list[TermUse]] = {}
    for u in uses:
        out.setdefault((u.node_id, u.char_span[0], u.char_span[1]), []).append(u)
    return out


def subject_of(rec: GoldenRecord, by_path: dict[str, Node]) -> Optional[tuple[str, int, int]]:
    span = rec.span
    if span is None:
        return None
    node_id = rec.node_id
    if node_id is None and rec.parent_path:
        node = by_path.get(rec.parent_path)
        node_id = node.id if node else None
    if node_id is None:
        return None
    return (node_id, span[0], span[1])


def find_use(subject: tuple[str, int, int],
             index: dict[tuple[str, int, int], list[TermUse]]) -> tuple[Optional[TermUse], str]:
    if subject in index:
        return index[subject][0], "exact"
    node_id, start, end = subject
    for (n, s, e), uses in index.items():
        if n == node_id and s < end and start < e:
            return uses[0], "overlapping span"
    return None, "not detected"


def population_summary(uses: Optional[list[TermUse]]) -> dict[str, Any]:
    if uses is None:
        return {"status": "no_data", "reason": "stage 4 term_uses absent"}
    return {
        "status": "measured",
        "term_uses_total": len(uses),
        "by_status": dict(Counter(u.status for u in uses).most_common()),
        "by_ambiguity_kind": dict(Counter(u.ambiguity_kind for u in uses).most_common()),
        "by_method": dict(Counter(u.method for u in uses).most_common()),
        "distinct_terms": len({u.term for u in uses}),
    }


def build(ctx: Context) -> Section:
    s = Section("golden_terms")
    uses = ctx.inputs.term_uses
    labels = ctx.golden.of_kind(TERM_KIND)
    s.data["pipeline_population"] = population_summary(uses)
    s.data["error_cost_weights"] = dict(config.ERROR_COSTS)
    s.data["error_cost_weights_note"] = ("declared placeholders in config.py for a domain "
                                         "expert to set; which error hurts more is a "
                                         "judgement about consequences, not about data")

    if uses is None or not labels:
        s.status = NO_DATA
        why = []
        if uses is None:
            why.append(f"stage 4 term_uses absent ({ctx.inputs.root}/vocab/term_uses.json)")
        if not labels:
            why.append(f"no golden term labels yet in {ctx.golden.directory}")
        s.reason = "; ".join(why)
        s.line(f"_{s.reason}. Nothing is scored._")
        pop = s.data["pipeline_population"]
        if pop["status"] == "measured":
            s.line()
            s.line(f"The pipeline produced **{pop['term_uses_total']}** term use(s) over "
                   f"{pop['distinct_terms']} distinct term(s):")
            s.table(["ambiguity kind", "count"],
                    [[k, v] for k, v in pop["by_ambiguity_kind"].items()])
            s.table(["status", "count"], [[k, v] for k, v in pop["by_status"].items()])
        return s

    index = use_index(uses)
    by_path = ctx.inputs.nodes_by_path()
    by_id = ctx.inputs.nodes_by_id()

    rows: list[dict[str, Any]] = []
    unresolved_subjects: list[dict[str, Any]] = []
    for rec in labels:
        subject = subject_of(rec, by_path)
        if subject is None:
            unresolved_subjects.append({"verdict": rec.verdict, "path": rec.path,
                                        "node_id": rec.node_id,
                                        "reason": "no resolvable (node, span) subject"})
            continue
        use, how = find_use(subject, index)
        node = by_id.get(subject[0])
        expected_term = rec.chosen_candidate or (use.term if use else None)
        rows.append({
            "subject": f"{(node.path if node else subject[0])}[{subject[1]}:{subject[2]}]",
            "verdict": rec.verdict,
            "expected_term": expected_term,
            "pipeline_term": use.term if use else None,
            "detected": use is not None,
            "match": how,
            "pipeline_ambiguity_kind": use.ambiguity_kind if use else None,
            "eval_ambiguity_kind": derive_ambiguity_kind(node, (subject[1], subject[2])),
            "pipeline_status": use.status if use else None,
            "reviewer": rec.reviewer,
        })

    gold_uses = [r for r in rows if r["verdict"] == "use"]
    gold_not_uses = [r for r in rows if r["verdict"] == "not_a_use"]

    true_positives = [r for r in gold_uses if r["detected"]
                      and (r["expected_term"] is None
                           or r["pipeline_term"] == r["expected_term"])]
    wrong_term = [r for r in gold_uses if r["detected"]
                  and r["expected_term"] is not None
                  and r["pipeline_term"] != r["expected_term"]]
    missed = [r for r in gold_uses if not r["detected"]]
    false_positives = [r for r in gold_not_uses if r["detected"]]

    # A wrong term is both: a use asserted that is not there, and a use missed.
    fp_rows = false_positives + wrong_term
    fn_rows = missed + wrong_term

    detection_recall = Rate(len(true_positives), len(gold_uses))
    precision = Rate(len(true_positives), len(true_positives) + len(fp_rows))

    per_kind: dict[str, dict[str, Any]] = {}
    for kind in AMBIGUITY_KINDS:
        per_kind[kind] = {"labels": 0, "true_positives": 0, "false_positives": 0,
                          "false_negatives": 0, "false_negative_kind_source": "eval_derived"}
    def bump(kind: Optional[str], field: str) -> None:
        k = kind if kind in per_kind else "none"
        per_kind[k][field] += 1
    for r in rows:
        bump(r["pipeline_ambiguity_kind"] or r["eval_ambiguity_kind"], "labels")
    for r in true_positives:
        bump(r["pipeline_ambiguity_kind"], "true_positives")
    for r in fp_rows:
        bump(r["pipeline_ambiguity_kind"], "false_positives")
    for r in fn_rows:
        bump(r["pipeline_ambiguity_kind"] or r["eval_ambiguity_kind"], "false_negatives")

    fp_cost = float(config.ERROR_COSTS.get("term_false_positive", 1.0))
    fn_cost = float(config.ERROR_COSTS.get("term_false_negative", 1.0))
    weighted = {
        "false_positives": len(fp_rows), "false_positive_weight": fp_cost,
        "false_negatives": len(fn_rows), "false_negative_weight": fn_cost,
        "weighted_cost": round(len(fp_rows) * fp_cost + len(fn_rows) * fn_cost, 3),
        "per_ambiguity_kind": {
            k: round(v["false_positives"] * fp_cost + v["false_negatives"] * fn_cost, 3)
            for k, v in per_kind.items()},
    }

    s.status = MEASURED
    s.data.update({
        "labels_scored": len(rows),
        "labels_with_no_resolvable_subject": unresolved_subjects,
        "detection_recall": detection_recall.as_dict(),
        "precision_over_labelled_spans": precision.as_dict(),
        "precision_note": "over labelled spans only, not a population precision",
        "confusion": {
            "true_positives": len(true_positives),
            "false_positives": len(fp_rows),
            "false_negatives": len(fn_rows),
            "wrong_term_counted_as_both": len(wrong_term),
        },
        "per_ambiguity_kind": per_kind,
        "cost_weighted_summary": weighted,
        "scored_rows": cap(rows, LIST_CAP)[0],
        "scored_rows_not_listed": cap(rows, LIST_CAP)[1],
    })

    s.line(f"Scored **{len(rows)}** golden term label(s).")
    s.line()
    s.table(["measure", "value"],
            [["detection recall", str(detection_recall)],
             ["precision (labelled spans only)", str(precision)],
             ["true positives", len(true_positives)],
             ["false positives", len(fp_rows)],
             ["false negatives", len(fn_rows)],
             ["wrong term (counted as both an FP and an FN)", len(wrong_term)]])
    s.line()
    s.line("**Per ambiguity kind.** False-negative kinds are eval-derived: there is no "
           "pipeline record to read one off, and `typo_dense` is a stage 4 section signal "
           "this harness cannot recompute.")
    s.table(["ambiguity kind", "labels", "TP", "FP", "FN", "weighted cost"],
            [[k, v["labels"], v["true_positives"], v["false_positives"],
              v["false_negatives"], weighted["per_ambiguity_kind"][k]]
             for k, v in per_kind.items()])
    s.line()
    s.line(f"**Cost-weighted summary**: {len(fp_rows)} FP x {fp_cost} + "
           f"{len(fn_rows)} FN x {fn_cost} = **{weighted['weighted_cost']}**. "
           f"Weights are declared placeholders in `config.ERROR_COSTS`.")
    if fp_rows or missed:
        s.line()
        s.table(["subject", "verdict", "expected term", "pipeline term", "kind"],
                [[r["subject"], r["verdict"], r["expected_term"], r["pipeline_term"],
                  r["pipeline_ambiguity_kind"] or f'{r["eval_ambiguity_kind"]} (derived)']
                 for r in cap(fp_rows + missed, LIST_CAP)[0]])
    if unresolved_subjects:
        s.bullet(f"{len(unresolved_subjects)} label(s) named a subject this run could not "
                 f"resolve; excluded from every rate above")
    return s
