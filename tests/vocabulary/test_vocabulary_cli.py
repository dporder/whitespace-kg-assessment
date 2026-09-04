"""Stage 4 end to end: `python -m pipeline.vocabulary` over the fixtures."""
from __future__ import annotations

import json
from pathlib import Path

import config
from pipeline.schemas import DefinitionSite, TermUse
from pipeline.vocabulary.__main__ import main

FIXTURES = config.ROOT / "fixtures"


def run(tmp_path: Path, *extra: str) -> tuple[int, Path]:
    code = main(["--input", "fixtures", "--fixtures-dir", str(FIXTURES),
                 "--output-dir", str(tmp_path), "--run", "t", "--quiet", *extra])
    return code, tmp_path / "t" / "vocab"


def test_the_cli_runs_clean_on_the_fixtures(tmp_path):
    code, vocab = run(tmp_path)
    assert code == 0
    assert not (vocab / "violations.json").exists()


def test_it_writes_the_files_stage_7_and_stage_8_read(tmp_path):
    _code, vocab = run(tmp_path)
    for name in ("definition_sites.json", "term_uses.json", "discovery_diff.json",
                 "typo_density.json", "routing.json", "audit_sample.json",
                 "definition_sites_provenance.json", "anomalies.json",
                 "summary.json"):
        assert (vocab / name).exists(), name


def test_the_outputs_validate_against_the_schemas(tmp_path):
    _code, vocab = run(tmp_path)
    sites = [DefinitionSite.model_validate(d)
             for d in json.loads((vocab / "definition_sites.json").read_text())]
    uses = [TermUse.model_validate(d)
            for d in json.loads((vocab / "term_uses.json").read_text())]
    assert sites and uses
    assert {s.source for s in sites} <= {"declared", "discovered", "both"}
    assert all(s.scope == "document" or s.scope.startswith("part:") for s in sites)
    assert all(u.definition_used in {None, "document"} or
               u.definition_used.startswith("part:") for u in uses)


def test_the_eval_harness_can_load_what_stage_4_wrote(tmp_path):
    """The seam that matters: stage 8 reads these two files by name and shape."""
    from pipeline.eval import inputs as eval_inputs
    _code, vocab = run(tmp_path)
    loaded = eval_inputs.load("output", vocab.parent, "t", [])
    by_kind = {r.kind: r for r in loaded.records}
    assert by_kind["definition_sites"].state == "loaded"
    assert by_kind["term_uses"].state == "loaded"
    assert loaded.definition_sites and loaded.term_uses


def test_every_span_reproduces_its_surface_text(tmp_path):
    """A term use whose offsets do not cut out the text they claim is a broken
    edge in the graph, so the stage checks it rather than counting it."""
    from pipeline.vocabulary import treeio
    _code, vocab = run(tmp_path)
    trees = treeio.load_trees("fixtures", FIXTURES, "t")
    by_id = trees.by_id()
    detail = json.loads((vocab / "routing.json").read_text())
    assert detail is not None
    for use in json.loads((vocab / "term_uses.json").read_text()):
        node = by_id[use["node_id"]]
        start, end = use["char_span"]
        field = node.title if use.get("ambiguity_kind") == "heading" else node.text
        assert field is not None
        assert 0 <= start < end <= len(field)


WRITTEN = ("definition_sites.json", "term_uses.json", "discovery_diff.json",
           "typo_density.json", "audit_sample.json", "summary.json")


def test_the_run_is_byte_identical_on_a_cold_rerun(tmp_path):
    """SPEC ground rule: the deterministic half of stage 4 is a pure function of
    its inputs and config. No clock, no dict-ordering leak, no global RNG."""
    import shutil
    _code, vocab = run(tmp_path)
    before = {name: (vocab / name).read_bytes() for name in WRITTEN}
    shutil.rmtree(tmp_path / "t")
    run(tmp_path)
    for name in WRITTEN:
        assert (vocab / name).read_bytes() == before[name], name


def test_only_the_recorded_paths_differ_between_two_output_roots(tmp_path):
    """A stronger determinism check than the rerun: nothing in the derived
    content may depend on where the run was written. The two absolute paths the
    summary records for provenance are the only permitted difference."""
    _a, vocab_a = run(tmp_path / "a")
    _b, vocab_b = run(tmp_path / "b")
    for name in WRITTEN:
        left = json.loads((vocab_a / name).read_text())
        right = json.loads((vocab_b / name).read_text())
        if name == "summary.json":
            for side in (left, right):
                side.pop("input_root")
                side["llm"].pop("cache_root")
        assert left == right, name


def test_a_batch_scopes_the_matching_but_not_the_vocabulary(tmp_path):
    """Vocabulary is inherited document-wide, so scoping to B1 must not throw
    away Joint Schedule 1's definitions: Core Terms would then report zero term
    uses purely because the definitions schedule is in a different batch, which
    is an artefact of the slicing rather than anything the document says."""
    code = main(["--input", "fixtures", "--fixtures-dir", str(FIXTURES),
                 "--output-dir", str(tmp_path), "--run", "t", "--quiet",
                 "--batch", "B1"])
    assert code == 0
    summary = json.loads((tmp_path / "t" / "vocab" / "summary.json").read_text())
    assert summary["parts"] == ["core-terms"]
    assert "joint-schedule-1" in summary["vocabulary_derived_from_parts"]
    assert summary["definition_sites"]["total"] > 0
    assert summary["term_uses"]["total"] > 0
    uses = json.loads((tmp_path / "t" / "vocab" / "term_uses.json").read_text())
    trees_by_id = {}
    from pipeline.vocabulary import treeio
    for _p, node in treeio.load_trees("fixtures", FIXTURES, "t",
                                      ["core-terms"]).nodes():
        trees_by_id[node.id] = node
    assert all(u["node_id"] in trees_by_id for u in uses), \
        "term uses are emitted only for the parts in scope"


def test_declared_and_discovered_are_reported_apart(tmp_path):
    _code, vocab = run(tmp_path)
    diff = json.loads((vocab / "discovery_diff.json").read_text())
    assert set(diff) >= {"declared", "discovered", "in_both",
                         "discovered_not_declared", "declared_not_discovered"}
    summary = json.loads((vocab / "summary.json").read_text())
    counts = summary["declared_vs_discovered"]
    assert counts["declared_terms"] == diff["declared"]["count"]
    assert counts["discovered_terms"] == diff["discovered"]["count"]


def test_the_summary_reports_counts_by_status_and_ambiguity_kind(tmp_path):
    _code, vocab = run(tmp_path)
    uses = summary_uses = json.loads((vocab / "summary.json").read_text())["term_uses"]
    assert sum(summary_uses["by_status"].values()) == uses["total"]
    assert sum(summary_uses["by_ambiguity_kind"].values()) == uses["total"]
    assert summary_uses["by_status"]["confident"] > 0


def test_the_audit_sample_is_drawn_and_stored(tmp_path):
    _code, vocab = run(tmp_path)
    sample = json.loads((vocab / "audit_sample.json").read_text())
    assert sample["config"]["strata"] == config.AUDIT["strata"]
    assert sample["sample"]["requested_sample_size"] == \
        config.AUDIT["confident_term_sample_size"]
    assert len(sample["items"]) == sample["sample"]["drawn_sample_size"]


def test_the_pending_llm_state_is_stated_not_hidden(tmp_path):
    """With no pipeline/llm.py the routed checks are queued. The output has to
    say so, rather than reporting a clean run that checked nothing."""
    _code, vocab = run(tmp_path)
    routing_out = json.loads((vocab / "routing.json").read_text())
    if not routing_out["llm"]["available"]:
        assert "pending llm.py" in routing_out["llm"]["note"]
        for queue in routing_out["queues"].values():
            assert queue["state"].startswith("pending")
            assert queue["verdicts"] == 0


def test_no_llm_still_produces_the_queues(tmp_path):
    code, vocab = run(tmp_path, "--no-llm")
    assert code == 0
    routing_out = json.loads((vocab / "routing.json").read_text())
    assert routing_out["queues"], "the queues are built even when nothing is called"


def test_source_ink_defects_are_recorded_never_repaired(tmp_path):
    _code, vocab = run(tmp_path)
    anomalies = json.loads((vocab / "anomalies.json").read_text())
    assert "never repaired" in anomalies["note"]
