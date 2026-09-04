"""`golden_refs`: detection and resolution scored separately, abstention scored.

SPEC 2.6: "detection recall and resolution precision reported as separate
numbers, plus abstention correctness, no golden unresolvable resolved."

Detection asks *did we find the pointing words at all*. Resolution asks *did we
point them at the right target*. They are different failures with different
fixes, so they are never multiplied into one number here. Abstention is scored
as a behaviour: a golden reference that genuinely has no target in the corpus is
supposed to stay unresolved with its candidates kept, and any of those that got
resolved anyway trips a zero-tolerance gate. Wrong confidence is the failure
mode this whole evaluation exists to prevent.

Every rate carries its counts. An empty golden set is `no_data`: the section
still prints what the pipeline produced, and the gates record
`skipped_no_data` rather than passing on nothing.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from pipeline.eval.context import Context, LIST_CAP
from pipeline.eval.golden import REF_KIND, GoldenRecord
from pipeline.eval.rates import MEASURED, NO_DATA, Rate, Section, cap
from pipeline.eval.text import similarity
from pipeline.schemas import Node


def ref_index(ctx: Context) -> dict[tuple[str, int, int], Node]:
    """(parent path, start, end) -> ref node."""
    out: dict[tuple[str, int, int], Node] = {}
    for part in sorted(ctx.inputs.refs):
        for r in ctx.inputs.refs[part]:
            if r.char_span is None:
                continue
            parent = r.path.rsplit("/ref@", 1)[0]
            out[(parent, r.char_span[0], r.char_span[1])] = r
    return out


def resolve_subject(rec: GoldenRecord, by_id: dict[str, Node]) -> Optional[tuple[str, int, int]]:
    """The (node path, span) a golden record is about, or None if unresolvable."""
    span = rec.span
    if span is None:
        return None
    path = rec.parent_path
    if path is None and rec.node_id:
        node = by_id.get(rec.node_id)
        path = node.path if node else None
    if path is None:
        return None
    return (path, span[0], span[1])


def find_ref(subject: tuple[str, int, int],
             index: dict[tuple[str, int, int], Node]) -> tuple[Optional[Node], str]:
    """Exact span match, then the best overlapping span on the same node.

    "Best" matters where one list phrase became several refs sharing a
    `group_id` ("Clauses 2.10, 9, 14"): a label covering the whole phrase
    overlaps all of them, and picking whichever the dict happened to yield first
    made the score depend on iteration order. The most-overlapping ref wins,
    ties broken by span so the choice is deterministic; when the winner belongs
    to a group that fact is reported, because a label spanning a whole group is
    a labelling question, not a resolution result.
    """
    if subject in index:
        return index[subject], "exact"
    path, start, end = subject
    overlaps = [((min(e, end) - max(s, start)), (s, e), ref)
                for (p, s, e), ref in index.items()
                if p == path and s < end and start < e]
    if not overlaps:
        return None, "not detected"
    _width, _span, ref = max(overlaps, key=lambda o: (o[0], -o[1][0], -o[1][1]))
    if ref.group_id and len(overlaps) > 1:
        return ref, (f"overlapping span, best of {len(overlaps)} refs split from one "
                     f"list phrase (group {ref.group_id})")
    return ref, "overlapping span"


def population_summary(ctx: Context) -> dict[str, Any]:
    refs = ctx.inputs.all_refs()
    return {
        "refs_total": len(refs),
        "by_status": dict(Counter(r.status for r in refs).most_common()),
        "by_ref_kind": dict(Counter(r.ref_kind for r in refs).most_common()),
        "by_resolver": dict(Counter(r.resolver for r in refs).most_common()),
        "with_candidates_kept": sum(1 for r in refs if r.candidates),
    }


def legislation_routing(ctx: Context) -> dict[str, Any]:
    """SPEC 2.2: how often near-miss legislation title routing fired.

    Whether it fires everywhere it *should* is a seeded-case question and lives
    in tests/eval/, not in a report over live data. What is measurable here is
    the population: distinct normalised keys, and pairs whose titles are close
    enough that one of them may be the other misspelled.
    """
    leg = [r for r in ctx.inputs.all_refs() if r.ref_kind == "legislation"]
    if not leg:
        return {"status": "no_data", "reason": "no legislation refs in scope"}
    keys = sorted({r.target_path for r in leg if r.target_path})
    near: list[dict[str, Any]] = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            score = similarity(a, b)
            if score >= 90.0:
                near.append({"a": a, "b": b, "similarity": score})
    routed = [r for r in leg if r.resolver in ("llm", "human")]
    return {"status": "measured", "legislation_refs": len(leg),
            "distinct_keys": len(keys),
            "near_miss_key_pairs": near[:LIST_CAP],
            "routed_to_llm_or_human": Rate(len(routed), len(leg)).as_dict()}


def build(ctx: Context) -> Section:
    s = Section("golden_refs")
    index = ref_index(ctx)
    by_id = ctx.inputs.nodes_by_id()
    labels = ctx.golden.of_kind(REF_KIND)

    s.data["pipeline_population"] = population_summary(ctx)
    s.data["golden"] = {"labels": len(labels), "directory": ctx.golden.directory,
                        "files": ctx.golden.files}
    s.data["legislation_near_miss_routing"] = legislation_routing(ctx)
    s.metrics.update({"reference_detection_recall": None,
                      "reference_resolution_precision": None,
                      "wrongly_resolved_unresolvables": None})

    if not ctx.inputs.refs:
        s.status = NO_DATA
        s.reason = f"no stage 3 refs loaded; looked in {ctx.inputs.root}/refs/"
    if not labels:
        s.status = NO_DATA
        s.reason = ((s.reason + "; ") if s.reason else "") + \
            (f"no golden ref labels yet in {ctx.golden.directory}"
             if ctx.golden.state != "absent"
             else f"golden directory absent: {ctx.golden.directory}")
        s.line(f"_{s.reason}. Nothing is scored; the gates that read this section "
               f"record skipped_no_data rather than passing._")
        s.line()
        pop = s.data["pipeline_population"]
        s.line(f"The pipeline produced **{pop['refs_total']}** ref(s) in scope:")
        s.table(["status", "count"], [[k, v] for k, v in pop["by_status"].items()])
        s.table(["ref_kind", "count"], [[k, v] for k, v in pop["by_ref_kind"].items()])
        return s

    unmatched_subject: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for rec in labels:
        subject = resolve_subject(rec, by_id)
        if subject is None:
            unmatched_subject.append({"verdict": rec.verdict, "path": rec.path,
                                      "node_id": rec.node_id,
                                      "reason": "no resolvable (node, span) subject"})
            continue
        ref, how = find_ref(subject, index)
        rows.append({
            "subject": f"{subject[0]}[{subject[1]}:{subject[2]}]",
            "verdict": rec.verdict, "chosen_candidate": rec.chosen_candidate,
            "detected": ref is not None, "match": how,
            "pipeline_status": ref.status if ref else None,
            "pipeline_target": ref.target_path if ref else None,
            "pipeline_ref_kind": ref.ref_kind if ref else None,
            "pipeline_resolver": ref.resolver if ref else None,
            "reviewer": rec.reviewer,
        })

    positives = [r for r in rows if r["verdict"] in ("target", "unresolvable")]
    negatives = [r for r in rows if r["verdict"] == "not_a_reference"]
    detected_positives = [r for r in positives if r["detected"]]
    boundary_mismatches = [r for r in detected_positives
                           if r["match"].startswith("overlapping span")]
    detection_recall = Rate(len(detected_positives), len(positives))
    false_positives = [r for r in negatives if r["detected"]]
    detection_precision = Rate(len(detected_positives),
                               len(detected_positives) + len(false_positives))

    targets = [r for r in positives if r["verdict"] == "target"]
    resolved_targets = [r for r in targets if r["detected"] and r["pipeline_target"]]
    correct = [r for r in resolved_targets if r["pipeline_target"] == r["chosen_candidate"]]
    resolution_precision = Rate(len(correct), len(resolved_targets))
    abstained_on_resolvable = [r for r in targets if r["detected"] and not r["pipeline_target"]]

    unresolvables = [r for r in positives if r["verdict"] == "unresolvable"]
    wrongly_resolved = [r for r in unresolvables if r["detected"] and r["pipeline_target"]]
    correctly_abstained = [r for r in unresolvables if r["detected"] and not r["pipeline_target"]]

    by_kind: dict[str, dict[str, int]] = {}
    for r in rows:
        kind = r["pipeline_ref_kind"] or "not detected"
        cell = by_kind.setdefault(kind, {"labels": 0, "detected": 0, "resolved_correct": 0,
                                         "resolved_wrong": 0, "wrongly_resolved_unresolvable": 0})
        cell["labels"] += 1
        cell["detected"] += 1 if r["detected"] else 0
        if r["verdict"] == "target" and r["detected"] and r["pipeline_target"]:
            key = ("resolved_correct" if r["pipeline_target"] == r["chosen_candidate"]
                   else "resolved_wrong")
            cell[key] += 1
        if r["verdict"] == "unresolvable" and r["detected"] and r["pipeline_target"]:
            cell["wrongly_resolved_unresolvable"] += 1

    s.status = MEASURED
    s.reason = None
    s.data.update({
        "labels_scored": len(rows),
        "labels_with_no_resolvable_subject": unmatched_subject,
        "detection": {
            "recall": detection_recall.as_dict(),
            "recall_note": "golden references the pipeline found, over all golden references",
            "precision_over_labelled_spans": detection_precision.as_dict(),
            "precision_note": "over labelled spans only: the golden set is a label set, "
                              "not an exhaustive annotation of any node, so this is not "
                              "a population precision",
            "false_positives": len(false_positives),
            "missed": [r["subject"] for r in positives if not r["detected"]][:LIST_CAP],
            "boundary_mismatches": len(boundary_mismatches),
        },
        "resolution": {
            "precision_on_resolved": resolution_precision.as_dict(),
            "precision_note": "over golden targets the pipeline actually resolved",
            "resolved": len(resolved_targets),
            "correct": len(correct),
            "wrong": [{"subject": r["subject"], "expected": r["chosen_candidate"],
                       "got": r["pipeline_target"], "resolver": r["pipeline_resolver"]}
                      for r in resolved_targets
                      if r["pipeline_target"] != r["chosen_candidate"]][:LIST_CAP],
            "abstained_on_a_resolvable_reference": len(abstained_on_resolvable),
        },
        "abstention": {
            "golden_unresolvables": len(unresolvables),
            "correctly_abstained": Rate(len(correctly_abstained), len(unresolvables)).as_dict(),
            "wrongly_resolved": Rate(len(wrongly_resolved), len(unresolvables)).as_dict(),
            "wrongly_resolved_count": len(wrongly_resolved),
            "wrongly_resolved_detail": [
                {"subject": r["subject"], "got": r["pipeline_target"],
                 "status": r["pipeline_status"], "resolver": r["pipeline_resolver"]}
                for r in wrongly_resolved][:LIST_CAP],
        },
        "by_ref_kind": by_kind,
        "scored_rows": cap(rows, LIST_CAP)[0],
        "scored_rows_not_listed": cap(rows, LIST_CAP)[1],
    })
    s.metrics.update({
        "reference_detection_recall": detection_recall,
        "reference_resolution_precision": resolution_precision,
        # Carried with its denominator, the number of golden unresolvables, so
        # "none went wrong" and "none were labelled" cannot look the same to the
        # zero-tolerance gate. See gates.GateSpec.basis.
        "wrongly_resolved_unresolvables": Rate(len(wrongly_resolved), len(unresolvables)),
    })

    s.line(f"Scored **{len(rows)}** golden ref label(s) from "
           f"{', '.join(ctx.golden.files) or ctx.golden.directory}.")
    s.line()
    s.table(["measure", "value", "what it answers"],
            [["detection recall", str(detection_recall),
              "did we find the pointing words at all"],
             ["detection precision (labelled spans only)", str(detection_precision),
              "of the spans a human labelled, how many we called correctly"],
             ["resolution precision (on resolved)", str(resolution_precision),
              "of the ones we resolved, how many point at the right target"],
             ["abstention correctness", str(Rate(len(correctly_abstained), len(unresolvables))),
              "golden unresolvables we correctly refused to resolve"],
             ["golden unresolvables wrongly resolved",
              str(Rate(len(wrongly_resolved), len(unresolvables))),
              "zero tolerance; no unresolvable labels means no verdict, not a pass"]])
    s.line()
    s.bullet(f"detection false positives on labelled spans: {len(false_positives)}")
    s.bullet(f"detected with different span boundaries: {len(boundary_mismatches)}")
    s.bullet(f"golden targets the pipeline abstained on: {len(abstained_on_resolvable)} "
             f"(a miss, not a wrong answer, and not counted in resolution precision)")
    if s.data["resolution"]["wrong"]:
        s.line()
        s.line("**Wrong targets**")
        s.table(["subject", "expected", "got", "resolver"],
                [[w["subject"], w["expected"], w["got"], w["resolver"]]
                 for w in s.data["resolution"]["wrong"]])
    if wrongly_resolved:
        s.line()
        s.line("**Golden unresolvables that got resolved anyway** (zero-tolerance gate)")
        s.table(["subject", "got", "status", "resolver"],
                [[w["subject"], w["got"], w["status"], w["resolver"]]
                 for w in s.data["abstention"]["wrongly_resolved_detail"]])
    if s.data["detection"]["missed"]:
        s.line()
        s.bullet("missed: " + ", ".join(s.data["detection"]["missed"]))
    s.line()
    s.line("**By ref kind**")
    s.table(["ref kind", "labels", "detected", "resolved correct", "resolved wrong",
             "unresolvable resolved"],
            [[k, v["labels"], v["detected"], v["resolved_correct"], v["resolved_wrong"],
              v["wrongly_resolved_unresolvable"]] for k, v in sorted(by_kind.items())])
    if unmatched_subject:
        s.bullet(f"{len(unmatched_subject)} label(s) named a subject this run could not "
                 f"resolve to a node and span; they are excluded from every rate above")
    return s
