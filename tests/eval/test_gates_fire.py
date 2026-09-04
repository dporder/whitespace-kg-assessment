"""Seeded failures. Every gate in config.GATES is proved to fire.

A gate nobody has watched fail is a gate nobody should trust, so each test here
breaks exactly one thing, asserts exit code 2, and asserts that the report names
the failing gate, the section it came from and the location. The green baseline
in `workspace` is asserted first, so a red result is the seed's doing and not
the fixtures'.
"""
from __future__ import annotations

import config
from pipeline.eval import gates as gates_mod
from pipeline.eval.rates import Rate

RESOLVED_REF = "core-terms/9/9.1/intro/ref@11-23"          # -> core-terms/3/3.1/3.1.2
GROUPED_REF_A = "core-terms/9/9.2/ref@94-99"               # -> core-terms/3/3.1/3.1.1
GROUPED_REF_B = "core-terms/9/9.2/ref@104-109"             # -> core-terms/3/3.1/3.1.2


def test_baseline_is_green(workspace):
    run = workspace.run()
    assert run.code == 0, run.failed_gates()
    assert run.failed_gates() == []
    assert run.section("invariants")["totals"]["unexplained"] == 0
    assert run.violations["violations"] == []


def test_every_config_gate_is_implemented():
    """A threshold in config.GATES that nothing enforces is worse than none."""
    assert set(config.GATES) <= set(gates_mod.GATE_SPECS), \
        f"unimplemented: {set(config.GATES) - set(gates_mod.GATE_SPECS)}"


# --------------------------------------------------- structural violations gate

def test_unexplained_geometric_violation_fails_the_structural_gate(workspace):
    """A child indented left of its parent, with nothing recorded about it."""
    workspace.mutate_node(
        "core-terms", "core-terms/3/3.1/3.1.2",
        bboxes_own=[{"page": 1, "bbox": [80.0, 215.0, 480.0, 245.0]}],
        bboxes_extent=[{"page": 1, "bbox": [80.0, 215.0, 480.0, 245.0]}])
    run = workspace.run()

    assert run.code == 2
    assert "structural_violations_unexplained_max" in run.failed_gates()
    gate = run.gate("structural_violations_unexplained_max")
    assert gate["section"] == "invariants"
    assert gate["observed_value"] >= 1

    checks = {c["check"]: c for c in run.section("invariants")["checks"]}
    assert checks["child_left_edge"]["unexplained"] == 1
    located = [v for v in run.section("invariants")["violations"]
               if v["check"] == "child_left_edge"]
    assert located and located[0]["path"] == "core-terms/3/3.1/3.1.2"
    assert located[0]["explained_by"] is None
    assert "**FAIL**" in run.markdown
    assert run.violations["exit_code"] == 2
    assert [v["gate"] for v in run.violations["violations"]] == \
        ["structural_violations_unexplained_max"]


def test_the_same_violation_recorded_as_an_anomaly_does_not_fail_the_gate(workspace):
    """Explained and unexplained are the whole distinction the gate rests on."""
    workspace.mutate_node(
        "core-terms", "core-terms/3/3.1/3.1.2",
        bboxes_own=[{"page": 1, "bbox": [80.0, 215.0, 480.0, 245.0]}],
        bboxes_extent=[{"page": 1, "bbox": [80.0, 215.0, 480.0, 245.0]}],
        anomalies=["child_left_edge_outdented: 3.1.2 is set left of 3.1 in the source",
                   "extent_nests_outdented: same cause"])
    run = workspace.run()

    invariants = run.section("invariants")
    assert invariants["totals"]["violations"] >= 1
    assert invariants["totals"]["unexplained"] == 0
    assert invariants["totals"]["explained_by_a_recorded_anomaly"] >= 1
    assert run.code == 0, run.failed_gates()


def test_sibling_vertical_overlap_is_caught(workspace):
    """9.2's box dragged up into 9.1's extent."""
    workspace.mutate_node(
        "core-terms", "core-terms/9/9.2",
        bboxes_own=[{"page": 2, "bbox": [86.0, 130.0, 490.0, 250.0]}],
        bboxes_extent=[{"page": 2, "bbox": [86.0, 130.0, 490.0, 250.0]}])
    run = workspace.run()
    violations = {v["check"] for v in run.section("invariants")["violations"]}
    assert "sibling_overlap" in violations
    assert run.code == 2


# --------------------------------------------------------- abstention gate

def test_a_golden_unresolvable_that_got_resolved_fails_the_zero_tolerance_gate(workspace):
    workspace.label(kind="ref", path=RESOLVED_REF, verdict="unresolvable",
                    note="no such target in the corpus")
    run = workspace.run()

    assert run.code == 2
    assert "wrongly_resolved_unresolvables_max" in run.failed_gates()
    gate = run.gate("wrongly_resolved_unresolvables_max")
    assert gate["threshold"] == 0
    assert gate["observed_value"] == 1
    abstention = run.section("golden_refs")["abstention"]
    assert abstention["wrongly_resolved"] == {"count": 1, "of": 1, "rate": 1.0}
    assert abstention["wrongly_resolved_count"] == 1
    assert abstention["wrongly_resolved_detail"][0]["got"] == "core-terms/3/3.1/3.1.2"
    # Detection was fine; the two numbers stay separate.
    assert run.section("golden_refs")["detection"]["recall"] == {"count": 1, "of": 1,
                                                                 "rate": 1.0}


def test_the_zero_tolerance_gate_skips_when_no_unresolvable_was_labelled(workspace):
    """A golden set with labels but no unresolvables measures nothing about
    abstention. A bare count of zero passed the max-0 gate green, which is the
    exact failure mode this harness exists to prevent."""
    workspace.label(kind="ref", path=RESOLVED_REF, verdict="target",
                    chosen_candidate="core-terms/3/3.1/3.1.2")
    run = workspace.run()

    gate = run.gate("wrongly_resolved_unresolvables_max")
    assert gate["status"] == "skipped_no_data", gate
    assert gate["counts"] == {"count": 0, "of": 0, "rate": None}
    assert "empty denominator" in gate["reason"]
    # The section body must say the same thing as the gates table.
    abstention = run.section("golden_refs")["abstention"]
    assert abstention["golden_unresolvables"] == 0
    assert abstention["wrongly_resolved"] == {"count": 0, "of": 0, "rate": None}
    assert "0/0 (no data)" in run.markdown


def test_a_correctly_abstained_unresolvable_passes(workspace):
    """The fixture's Framework Schedule 4 ref is unresolved with candidates kept."""
    workspace.label(kind="ref", path="core-terms/9/9.1/intro/ref@28-71",
                    verdict="unresolvable")
    run = workspace.run()
    assert run.code == 0, run.failed_gates()
    abstention = run.section("golden_refs")["abstention"]
    assert abstention["wrongly_resolved"] == {"count": 0, "of": 1, "rate": 0.0}
    assert abstention["correctly_abstained"] == {"count": 1, "of": 1, "rate": 1.0}


# ------------------------------------------------------ resolution precision gate

def test_resolution_precision_below_threshold_fails(workspace):
    workspace.label(kind="ref", path=RESOLVED_REF, verdict="target",
                    chosen_candidate="core-terms/3/3.1/3.1.2")
    workspace.label(kind="ref", path=GROUPED_REF_A, verdict="target",
                    chosen_candidate="core-terms/3/3.1/3.1.1")
    workspace.label(kind="ref", path=GROUPED_REF_B, verdict="target",
                    chosen_candidate="core-terms/9/9.1")        # pipeline says 3.1.2
    run = workspace.run()

    assert run.code == 2
    assert "reference_precision_min" in run.failed_gates()
    gate = run.gate("reference_precision_min")
    assert gate["counts"] == {"count": 2, "of": 3, "rate": 2 / 3}
    assert "2/3" in gate["observed"], "a rate is never printed without its counts"
    wrong = run.section("golden_refs")["resolution"]["wrong"]
    assert wrong[0]["expected"] == "core-terms/9/9.1"
    assert wrong[0]["got"] == "core-terms/3/3.1/3.1.2"
    # Detection is unaffected, which is the point of reporting them apart.
    assert run.gate("detection_recall_min")["status"] == "pass"


# ---------------------------------------------------------- detection recall gate

def test_a_reference_the_pipeline_never_found_fails_detection_recall(workspace):
    """A human labels a citation at a span with no ref: a detection miss."""
    workspace.label(kind="ref", path="core-terms/9/9.2/ref@0-3", verdict="target",
                    chosen_candidate="core-terms/3", note="seeded missed citation")
    run = workspace.run()

    assert run.code == 2
    assert "detection_recall_min" in run.failed_gates()
    detection = run.section("golden_refs")["detection"]
    assert detection["recall"] == {"count": 0, "of": 1, "rate": 0.0}
    assert detection["missed"] == ["core-terms/9/9.2[0:3]"]
    # Nothing was resolved wrongly, so that gate stays out of it.
    assert run.gate("reference_precision_min")["status"] == "skipped_no_data"


def test_a_detection_false_positive_is_counted_but_recall_is_not_hurt(workspace):
    workspace.label(kind="ref", path=RESOLVED_REF, verdict="not_a_reference")
    run = workspace.run()
    detection = run.section("golden_refs")["detection"]
    assert detection["false_positives"] == 1
    assert detection["recall"] == {"count": 0, "of": 0, "rate": None}
    assert run.gate("detection_recall_min")["status"] == "skipped_no_data"


# ------------------------------------------------------------- audit gate, unit

def test_audit_gate_fails_below_threshold_and_skips_without_data():
    """The audit's own input needs pipeline/llm.py, which does not exist yet, so
    this exercises the gate arithmetic directly rather than faking a checker."""
    failing = gates_mod.evaluate(
        {"stratified_audit_agreement_min": 0.90},
        {"stratified_audit_agreement": Rate(8, 10)})
    assert failing[0].status == "fail"
    assert failing[0].observed == "8/10 (0.800)"
    assert gates_mod.exit_code(failing) == 2

    passing = gates_mod.evaluate(
        {"stratified_audit_agreement_min": 0.90},
        {"stratified_audit_agreement": Rate(19, 20)})
    assert passing[0].status == "pass"

    absent = gates_mod.evaluate(
        {"stratified_audit_agreement_min": 0.90},
        {"stratified_audit_agreement": None},
        {"stratified_audit": "audit runner pending llm.py"})
    assert absent[0].status == "skipped_no_data"
    assert "pending llm.py" in absent[0].reason
    assert gates_mod.exit_code(absent) == 0


def test_a_rate_over_an_empty_denominator_never_passes_a_gate():
    results = gates_mod.evaluate({"detection_recall_min": 0.95},
                                 {"reference_detection_recall": Rate(0, 0)})
    assert results[0].status == "skipped_no_data"
    assert "empty denominator" in results[0].reason
    assert gates_mod.exit_code(results) == 0


def test_an_unimplemented_gate_in_config_exits_two():
    results = gates_mod.evaluate({"some_future_gate_min": 0.8}, {})
    assert results[0].status == "unimplemented"
    assert gates_mod.exit_code(results) == 2
    assert "worse than no threshold" in results[0].reason


def test_violations_file_is_written_even_when_everything_passes(workspace):
    run = workspace.run()
    assert run.violations["violations"] == []
    assert run.violations["exit_code"] == 0
    assert len(run.violations["gates"]) == len(config.GATES)
