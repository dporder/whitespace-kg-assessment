"""The report's shape is a contract: SPEC 2.6 section names, and honest absence."""
from __future__ import annotations

import json
import re
import shutil

from pipeline.eval.sections import SECTION_NAMES

SPEC_2_6_SECTIONS = [
    "invariants",
    "page_map_vs_provided",
    "outline_vs_provided",
    "definitions_vs_provided",
    "golden_refs",
    "golden_terms",
    "stratified_audit",
    "confidence_calibration",
    "resolution_transitions",
    "concepts",
]


def test_section_names_are_exactly_the_spec_list():
    """Written out longhand on purpose: if SPEC 2.6 changes, this fails first."""
    assert SECTION_NAMES == SPEC_2_6_SECTIONS


def test_report_carries_every_section_in_order(workspace):
    run = workspace.run()
    assert list(run.report["sections"]) == SPEC_2_6_SECTIONS
    for name in SPEC_2_6_SECTIONS:
        assert f"\n## {name}\n" in run.markdown, f"{name} missing from report.md"


def test_both_report_files_are_written_even_with_nothing_to_measure(tmp_path):
    """An empty workspace still gets a complete report, not a crash."""
    from pipeline.eval.__main__ import main
    empty = tmp_path / "empty"
    empty.mkdir()
    code = main(["--input", "fixtures", "--fixtures-dir", str(empty),
                 "--output-dir", str(tmp_path / "output"),
                 "--golden-dir", str(tmp_path / "golden"),
                 "--no-pdf", "--no-llm", "--quiet"])
    eval_dir = tmp_path / "output" / "dev" / "eval"
    report = json.loads((eval_dir / "report.json").read_text())
    assert (eval_dir / "report.md").exists()
    assert list(report["sections"]) == SPEC_2_6_SECTIONS
    assert code == 0, "no data must not fail a gate"
    for name, section in report["sections"].items():
        assert section["status"] in ("no_data", "partial"), name
        assert section.get("reason"), f"{name} degraded without saying why"
    for gate in report["gates"]["results"]:
        assert gate["status"] == "skipped_no_data", gate


def test_no_clock_reading_anywhere_in_the_report(workspace):
    """EVALUATION.md s4: regression is a diff between reports. A clock would
    make every diff non-empty."""
    run = workspace.run()

    def keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from keys(v)

    assert not {k for k in keys(run.report)} & {
        "generated_at", "timestamp", "created_at", "ran_at", "now"}
    datetime_like = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
    assert not datetime_like.search(json.dumps(run.report))
    assert not datetime_like.search(run.markdown)
    assert run.report["inputs_fingerprint"]["combined"]


def test_two_cold_runs_over_identical_inputs_are_byte_identical(workspace):
    """The whole file, transitions included. This is the claim the report header
    makes, so it is pinned rather than described."""
    first = workspace.run()
    first_bytes = (first.eval_dir / "report.json").read_bytes()
    first_md = (first.eval_dir / "report.md").read_bytes()

    shutil.rmtree(workspace.output)                # no prior snapshot, as on run one
    second = workspace.run()

    assert (second.eval_dir / "report.json").read_bytes() == first_bytes
    assert (second.eval_dir / "report.md").read_bytes() == first_md


def test_consecutive_runs_differ_only_where_the_harness_is_stateful(workspace):
    """resolution_transitions reads the snapshot the previous run wrote, so a
    second run in the same output directory legitimately reports history the
    first did not have. Nothing else may move."""
    first = workspace.run().report
    second = workspace.run().report
    for name in SPEC_2_6_SECTIONS:
        if name == "resolution_transitions":
            continue
        assert json.dumps(first["sections"][name], sort_keys=True) == \
            json.dumps(second["sections"][name], sort_keys=True), name
    assert first["inputs_fingerprint"] == second["inputs_fingerprint"]
    assert first["sections"]["resolution_transitions"]["status"] == "no_data"
    assert second["sections"]["resolution_transitions"]["status"] == "measured"


def test_the_report_header_does_not_overstate_determinism(workspace):
    """The unqualified claim was wrong: consecutive runs are not byte-identical."""
    markdown = workspace.run().markdown
    assert "identical prior snapshot state" in markdown
    assert "resolution_transitions`, which is stateful by design" in markdown


def test_absent_and_failed_inputs_are_different_states(workspace):
    (workspace.fixtures / "tree" / "core-terms.json").write_text("{ not json")
    run = workspace.run()
    failed = run.report["input_source"]["failed"]
    assert any(f["key"] == "core-terms" and "JSONDecodeError" in f["error"] for f in failed)
    assert run.section("invariants")["status"] == "partial"
    # The other two parts still get checked rather than the run collapsing.
    assert set(run.section("invariants")["parts_checked"]) == {"award-form",
                                                               "joint-schedule-1"}


def test_scope_flags(workspace):
    default = workspace.run().report["scope"]
    assert default["mode"] == "present"
    assert set(default["parts"]) == {"award-form", "core-terms", "joint-schedule-1"}
    assert default["cross_checks"] == "in_scope_parts"

    batched = workspace.run("--batch", "B1").report["scope"]
    assert batched["mode"] == "batch:B1"
    assert batched["parts"] == ["core-terms"]

    full = workspace.run("--full").report["scope"]
    assert full["full"] is True
    assert full["cross_checks"] == "whole_document"
