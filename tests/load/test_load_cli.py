"""Stage 7 end to end, without a database: rows, export, audit, reconciliation."""
from __future__ import annotations

import json

from pipeline.load.__main__ import main


def run(tmp_path, *extra):
    return main(["--input", "fixtures", "--run", "t", "--no-neo4j", "--quiet",
                 "--output-dir", str(tmp_path), *extra])


def report_of(tmp_path) -> dict:
    return json.loads((tmp_path / "t" / "graph" / "load_report.json").read_text())


def rows_of(tmp_path, name) -> list[dict]:
    path = tmp_path / "t" / "graph" / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_the_cli_writes_every_artefact(tmp_path):
    assert run(tmp_path, "--batch", "B1") == 0
    graph_dir = tmp_path / "t" / "graph"
    for name in ("nodes.jsonl", "edges.jsonl", "graph.json", "load_report.json",
                 "audit.jsonl"):
        assert (graph_dir / name).exists(), name


def test_counts_reconcile_with_the_stage_outputs(tmp_path):
    """EVALUATION 4 makes a count that does not reconcile a release gate."""
    run(tmp_path, "--batch", "B1")
    rec = report_of(tmp_path)["reconciliation"]
    assert rec["reconciles"] is True
    assert (rec["tree_and_ref_nodes_written"] ==
            rec["stage_inputs"]["tree_nodes"] + rec["stage_inputs"]["refs"]
            + rec["stage_inputs"]["document_root"])
    assert len(rows_of(tmp_path, "nodes.jsonl")) == rec["graph_rows"]["nodes"]
    assert len(rows_of(tmp_path, "edges.jsonl")) == rec["graph_rows"]["edges"]


def test_every_row_is_batch_tagged(tmp_path):
    run(tmp_path, "--batch", "B1")
    assert all(row["batch_id"] == "B1" for row in rows_of(tmp_path, "nodes.jsonl"))
    assert all(row["batch_id"] == "B1" for row in rows_of(tmp_path, "edges.jsonl"))


def test_the_edge_types_are_the_ones_spec_2_5_lists(tmp_path):
    run(tmp_path, "--parts", "core-terms,joint-schedule-1,award-form", "--batch", "B1")
    allowed = {"CONTAINS", "NEXT", "RESOLVES_TO", "CANDIDATE", "USES_TERM", "DEFINED_IN",
               "ABOUT", "DEFINED_USING", "CONCEPT_REL", "ASSOCIATED_TERM", "SUPERSEDES"}
    seen = {row["type"] for row in rows_of(tmp_path, "edges.jsonl")}
    assert seen <= allowed
    assert {"CONTAINS", "NEXT", "RESOLVES_TO", "USES_TERM", "DEFINED_IN", "ABOUT",
            "DEFINED_USING", "ASSOCIATED_TERM"} <= seen


def test_the_document_root_is_synthesised_and_flagged(tmp_path):
    """Stage 2 writes one file per part and no document root, so the loader
    mints the node those CONTAINS edges start from, and says that it did."""
    run(tmp_path, "--batch", "B1")
    report = report_of(tmp_path)
    assert any(n["kind"] == "document_root_synthesised" for n in report["notes"])
    root = next(r for r in rows_of(tmp_path, "nodes.jsonl")
                if r["labels"][1] == "Document")
    assert any("synthesised" in a for a in root["props"]["anomalies"])


def test_the_audit_log_records_the_load(tmp_path):
    run(tmp_path, "--batch", "B1")
    entries = rows_of(tmp_path, "audit.jsonl")
    assert entries, "nothing was audited"
    assert all({"batch_id", "op", "reason", "counts"} <= set(e) for e in entries)


def test_the_export_says_what_it_covers(tmp_path):
    run(tmp_path, "--batch", "B1")
    data = json.loads((tmp_path / "t" / "graph" / "graph.json").read_text())
    assert data["graph"]["batch_id"] == "B1"
    assert data["graph"]["parts"] == ["core-terms"]
    assert data["multigraph"] is True


def test_salience_is_reported_with_its_formula_and_exclusions(tmp_path):
    run(tmp_path, "--parts", "core-terms,joint-schedule-1,award-form", "--batch", "B1")
    s = report_of(tmp_path)["salience"]
    assert s["formula"] == "salience = breadth * log(1 + frequency)"
    assert s["nodes_scored"] > 0
    assert "furniture_nodes_excluded" in s
    assert s["config_keys_requested"]


def test_an_access_label_is_inherited_by_every_node(tmp_path):
    run(tmp_path, "--batch", "B1", "--access-label", "OFFICIAL-SENSITIVE")
    assert all(row["props"].get("access_label") == "OFFICIAL-SENSITIVE"
               for row in rows_of(tmp_path, "nodes.jsonl")
               if "Node" in row["labels"])


def test_the_load_is_deterministic(tmp_path):
    run(tmp_path, "--batch", "B1")
    first = (tmp_path / "t" / "graph" / "nodes.jsonl").read_bytes()
    run(tmp_path, "--batch", "B1")
    assert (tmp_path / "t" / "graph" / "nodes.jsonl").read_bytes() == first


def test_absent_enrichment_stages_name_the_edge_types_they_starve(tmp_path):
    """A load over trees and refs alone is legitimate, but "no ASSOCIATED_TERM
    edges" must never read as "the join found nothing"."""
    import shutil

    import config

    source = tmp_path / "src"
    (source / "tree").mkdir(parents=True)
    (source / "refs").mkdir(parents=True)
    shutil.copy(config.ROOT / "fixtures" / "tree" / "core-terms.json",
                source / "tree" / "core-terms.json")
    shutil.copy(config.ROOT / "fixtures" / "refs" / "core-terms.json",
                source / "refs" / "core-terms.json")
    assert main(["--input", "fixtures", "--run", "t", "--no-neo4j", "--quiet",
                 "--output-dir", str(tmp_path), "--fixtures-dir", str(source),
                 "--batch", "B1"]) == 0
    skipped = report_of(tmp_path)["reconciliation"]["edge_types_skipped"]
    by_type = {s["edge_type"]: s for s in skipped}
    assert {"USES_TERM", "DEFINED_IN", "DEFINED_USING", "ABOUT", "CONCEPT_REL",
            "ASSOCIATED_TERM"} <= set(by_type)
    assert by_type["ASSOCIATED_TERM"]["needs"] == "stages 4 and 5"
    assert "concepts.json" in by_type["ABOUT"]["missing_input"]


def test_nothing_is_reported_as_skipped_when_the_inputs_are_there(tmp_path):
    run(tmp_path, "--parts", "core-terms,joint-schedule-1,award-form", "--batch", "B1")
    skipped = {s["edge_type"]
               for s in report_of(tmp_path)["reconciliation"]["edge_types_skipped"]}
    assert "ASSOCIATED_TERM" not in skipped
    assert "USES_TERM" not in skipped
