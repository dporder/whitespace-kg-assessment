"""Release gates. Thresholds live in `config.GATES`, the logic lives here.

SPEC section 3: the CLI exits 2 when a gate fails, still writing its output plus
a violations file. Three rules make the gates trustworthy rather than decorative:

1. **A gate never fires on missing data.** No golden labels means
   `skipped_no_data`, not a pass and not a failure. A harness that passes on an
   empty denominator is the confident wrongness this project exists to prevent.
2. **A gate in config that this harness does not implement is a failure.**
   Silently ignoring a threshold someone deliberately set is worse than
   stopping, so an unknown key in `config.GATES` exits 2 and says which key.
3. **Every observed value is printed with its absolute counts**, because a
   threshold met at 9 of 10 and one met at 900 of 1000 are different facts.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pipeline.eval.rates import Rate

PASS, FAIL, SKIPPED, UNIMPLEMENTED = "pass", "fail", "skipped_no_data", "unimplemented"


@dataclass(frozen=True)
class GateSpec:
    metric: str
    op: str                      # ">=" or "<="
    section: str
    question: str
    basis: str = "rate"          # "rate" compares the ratio, "count" the numerator

# A "count" basis exists so a zero-tolerance threshold can still be a Rate. The
# denominator is what makes "no labels yet" distinguishable from "nothing went
# wrong": a bare zero passes a max-0 gate whether or not anything was measured,
# which is the failure mode this whole harness is built to prevent. Carrying the
# denominator routes the empty case to skipped_no_data and compares the count.


# config.GATES key -> what it reads and how it compares.
GATE_SPECS: dict[str, GateSpec] = {
    "reference_precision_min": GateSpec(
        "reference_resolution_precision", ">=", "golden_refs",
        "of the golden references we resolved, how many point at the right target"),
    "wrongly_resolved_unresolvables_max": GateSpec(
        "wrongly_resolved_unresolvables", "<=", "golden_refs",
        "golden references with no correct target that we resolved anyway; zero tolerance",
        basis="count"),
    "structural_violations_unexplained_max": GateSpec(
        "structural_violations_unexplained", "<=", "invariants",
        "structural or geometric violations with no recorded anomaly explaining them",
        basis="count"),
    "detection_recall_min": GateSpec(
        "reference_detection_recall", ">=", "golden_refs",
        "golden references whose pointing words we found at all"),
    "stratified_audit_agreement_min": GateSpec(
        "stratified_audit_agreement", ">=", "stratified_audit",
        "independent checker agreement on a stratified sample of confident decisions"),
}


@dataclass
class GateResult:
    name: str
    threshold: Any
    status: str
    section: Optional[str] = None
    metric: Optional[str] = None
    question: Optional[str] = None
    observed: Optional[str] = None
    observed_value: Optional[float] = None
    counts: Optional[dict] = None
    reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        out = {"gate": self.name, "threshold": self.threshold, "status": self.status,
               "section": self.section, "metric": self.metric, "question": self.question,
               "observed": self.observed, "observed_value": self.observed_value}
        if self.counts is not None:
            out["counts"] = self.counts
        if self.reason:
            out["reason"] = self.reason
        return out


def _compare(observed: float, op: str, threshold: float) -> bool:
    return observed >= threshold if op == ">=" else observed <= threshold


def evaluate(gates: dict[str, Any], metrics: dict[str, Any],
             section_reasons: Optional[dict[str, Optional[str]]] = None) -> list[GateResult]:
    """One GateResult per key in config.GATES, in config order."""
    section_reasons = section_reasons or {}
    results: list[GateResult] = []
    for name, threshold in gates.items():
        spec = GATE_SPECS.get(name)
        if spec is None:
            results.append(GateResult(
                name=name, threshold=threshold, status=UNIMPLEMENTED,
                reason=(f"config.GATES defines {name!r} but this harness implements no "
                        f"check for it. A threshold someone set and nothing enforces is "
                        f"worse than no threshold, so this exits 2 rather than passing.")))
            continue
        value = metrics.get(spec.metric)
        result = GateResult(name=name, threshold=threshold, status=SKIPPED,
                            section=spec.section, metric=spec.metric,
                            question=spec.question)
        if value is None:
            result.reason = (section_reasons.get(spec.section)
                             or f"{spec.section} produced no measurement for {spec.metric}")
            results.append(result)
            continue
        if isinstance(value, Rate):
            result.observed = str(value)
            result.counts = value.as_dict()
            if not value.has_data:
                result.reason = (f"{spec.metric} has an empty denominator: "
                                 f"{value}. A rate over nothing is unknown, not zero "
                                 f"and not one, and a max-0 gate must not pass on it.")
                results.append(result)
                continue
            result.observed_value = float(value.count) if spec.basis == "count" \
                else value.rate
        else:
            result.observed_value = float(value)
            result.observed = str(value)
        result.status = PASS if _compare(result.observed_value, spec.op,
                                         float(threshold)) else FAIL
        if result.status == FAIL:
            result.reason = (f"{result.observed} {'<' if spec.op == '>=' else '>'} "
                             f"threshold {threshold}")
        results.append(result)
    return results


def exit_code(results: list[GateResult]) -> int:
    """0 when nothing failed. 2 on any failure or unimplemented gate (SPEC section 3)."""
    return 2 if any(r.status in (FAIL, UNIMPLEMENTED) for r in results) else 0


def write_violations(path: Path, run: str, results: list[GateResult],
                     extra: Optional[dict] = None) -> Path:
    """Always written, so a stale file from a previous failing run cannot mislead."""
    failed = [r for r in results if r.status in (FAIL, UNIMPLEMENTED)]
    payload = {
        "run": run,
        "exit_code": exit_code(results),
        "violations": [r.as_dict() for r in failed],
        "gates": [r.as_dict() for r in results],
    }
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    return path
