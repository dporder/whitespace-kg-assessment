"""`confidence_calibration`: reliability table per resolver, bucket vs precision.

EVALUATION.md layer 5: "every confidence number must have met ground truth". An
LLM resolver emits a raw score with its ranked candidates; this section buckets
those raw scores per resolver and puts the observed precision from the golden
labels next to each bucket. A threshold like "auto accept above 0.9" only means
something once that table exists.

Two things are printed even before labels exist, because they are the useful
half tonight:

- **Bucket populations.** How many decisions each resolver placed in each
  score band. EVALUATION.md section 6 lists the counts as tonight's deliverable
  and the precision column as written-down-not-run.
- **Measured precision per deterministic resolver class.** SPEC 2.4 says a
  deterministic resolver does not assert its own confidence; it carries the
  empirically measured precision of its class, attached at load time. That
  number is produced here, or reported as not yet measurable.

`stated_confidence_exceeds_observed_precision` marks any bucket where the score
promises more than the labels deliver, which is the release check in
EVALUATION.md section 4.
"""
from __future__ import annotations

from typing import Any, Optional

from pipeline.eval.context import Context, LIST_CAP
from pipeline.eval.golden import REF_KIND
from pipeline.eval.rates import MEASURED, NO_DATA, PARTIAL, Rate, Section
from pipeline.eval.sections.golden_refs import ref_index, resolve_subject, find_ref

BUCKETS = [(i / 10, (i + 1) / 10) for i in range(10)]
OVERCONFIDENCE_MARGIN = 0.10      # spec-silent; the declared margin of EVALUATION.md s4


def bucket_of(score: float) -> tuple[float, float]:
    for lo, hi in BUCKETS:
        if lo <= score < hi:
            return (lo, hi)
    return BUCKETS[-1]


def build(ctx: Context) -> Section:
    s = Section("confidence_calibration")
    refs = ctx.inputs.all_refs()
    if not refs:
        s.status = NO_DATA
        s.reason = "no stage 3 refs loaded; no confidence scores to calibrate"
        s.line(f"_{s.reason}_")
        return s

    # -- golden verdicts keyed by the ref they judge -----------------------------
    index = ref_index(ctx)
    by_id = ctx.inputs.nodes_by_id()
    verdict_for: dict[str, tuple[str, Optional[str]]] = {}
    for rec in ctx.golden.of_kind(REF_KIND):
        subject = resolve_subject(rec, by_id)
        if subject is None:
            continue
        ref, _how = find_ref(subject, index)
        if ref is not None:
            verdict_for[ref.path] = (rec.verdict, rec.chosen_candidate)

    table: dict[tuple[str, tuple[float, float]], dict[str, Any]] = {}
    scored_refs = [r for r in refs if r.confidence is not None]
    for r in scored_refs:
        key = (r.resolver or "unknown", bucket_of(float(r.confidence)))
        cell = table.setdefault(key, {"decisions": 0, "labelled": 0, "correct": 0})
        cell["decisions"] += 1
        verdict = verdict_for.get(r.path)
        if verdict is None:
            continue
        cell["labelled"] += 1
        kind, chosen = verdict
        if kind == "target" and r.target_path == chosen:
            cell["correct"] += 1
        elif kind == "unresolvable" and not r.target_path:
            cell["correct"] += 1

    rows: list[dict[str, Any]] = []
    for (resolver, (lo, hi)), cell in sorted(table.items()):
        observed = Rate(cell["correct"], cell["labelled"])
        overconfident = (observed.rate is not None and lo - observed.rate > OVERCONFIDENCE_MARGIN)
        rows.append({"resolver": resolver, "bucket": f"[{lo:.1f}, {hi:.1f})",
                     "decisions": cell["decisions"],
                     "observed_precision": observed.as_dict(),
                     "stated_confidence_exceeds_observed_precision": overconfident})

    # -- what a deterministic resolver class should carry at load time -----------
    class_precision: dict[str, Any] = {}
    for resolver in sorted({r.resolver for r in refs if r.resolver}):
        judged = [r for r in refs if r.resolver == resolver and r.path in verdict_for]
        correct = 0
        for r in judged:
            kind, chosen = verdict_for[r.path]
            if kind == "target" and r.target_path == chosen:
                correct += 1
            elif kind == "unresolvable" and not r.target_path:
                correct += 1
        rate = Rate(correct, len(judged))
        class_precision[resolver] = {
            "measured_precision": rate.as_dict(),
            "attachable_at_load": rate.rate is not None,
            "note": None if rate.has_data else
            "no golden label has met this resolver yet; a confidence attached now "
            "would be a vibe, not a number",
        }

    unscored = [r for r in refs if r.confidence is None]
    s.status = MEASURED if verdict_for else (PARTIAL if scored_refs else NO_DATA)
    if not verdict_for:
        s.reason = ("no golden ref labels have met a scored decision yet, so the "
                    "observed-precision column is empty by design; bucket populations "
                    "are real")
    s.data.update({
        "overconfidence_margin": OVERCONFIDENCE_MARGIN,
        "refs_with_a_raw_score": len(scored_refs),
        "refs_without_a_raw_score": len(unscored),
        "refs_without_a_raw_score_note": "deterministic resolvers do not assert their own "
                                         "confidence (SPEC 2.4); they carry the measured "
                                         "precision of their class, below",
        "reliability_table": rows,
        "measured_precision_by_resolver": class_precision,
        "golden_labels_meeting_a_ref": len(verdict_for),
    })

    s.line(f"**{len(scored_refs)}** ref(s) carry a raw score; **{len(unscored)}** do not, "
           f"which is correct for deterministic resolvers.")
    if s.reason:
        s.line()
        s.line(f"_{s.reason}_")
    s.line()
    s.line("**Reliability table**")
    s.table(["resolver", "raw score bucket", "decisions", "observed precision", "overconfident"],
            [[r["resolver"], r["bucket"], r["decisions"],
              str(Rate(r["observed_precision"]["count"], r["observed_precision"]["of"])),
              "**yes**" if r["stated_confidence_exceeds_observed_precision"] else "no"]
             for r in rows[:LIST_CAP]])
    s.line()
    s.line("**Precision to attach to each resolver class at load time**")
    s.table(["resolver", "measured precision", "attachable", "note"],
            [[k, str(Rate(v["measured_precision"]["count"], v["measured_precision"]["of"])),
              "yes" if v["attachable_at_load"] else "no", v["note"] or ""]
             for k, v in class_precision.items()])
    return s
