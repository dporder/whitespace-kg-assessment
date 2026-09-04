"""Stage 5 end to end: `python -m pipeline.concepts` over the fixtures."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import config
from pipeline.concepts.__main__ import main
from pipeline.schemas import Concept
from pipeline.vocabulary import llmio
from tests.concepts.conftest import FakeClaude, concept, install_llm, reply

FIXTURES = config.ROOT / "fixtures"


def run(tmp_path: Path, *extra: str) -> tuple[int, Path]:
    code = main(["--input", "fixtures", "--fixtures-dir", str(FIXTURES),
                 "--output-dir", str(tmp_path), "--run", "t", "--quiet",
                 "--no-embed", *extra])
    return code, tmp_path / "t"


def fixture_reply(_task, prompt):
    """One concept per unit, claiming the first provision the prompt listed."""
    listing = prompt.split("PROVISIONS (path :: text):\n", 1)[1]
    first = listing.split("\n", 1)[0].split(" :: ", 1)[0]
    unit = prompt.split("UNIT ", 1)[1].split(" ", 1)[0]
    return reply(concept(f"theme of {unit}", 0.72, [first]))


def test_the_cli_runs_clean_and_writes_where_the_spec_says(tmp_path, monkeypatch):
    install_llm(monkeypatch, FakeClaude(fixture_reply))
    code, run_dir = run(tmp_path)
    assert code == 0
    assert (run_dir / "concepts.json").exists()
    for name in ("scan.json", "resolution.json", "summary.json"):
        assert (run_dir / "concepts" / name).exists(), name


def test_the_output_validates_and_the_eval_harness_can_load_it(tmp_path, monkeypatch):
    from pipeline.eval import inputs as eval_inputs
    install_llm(monkeypatch, FakeClaude(fixture_reply))
    _code, run_dir = run(tmp_path)
    concepts = [Concept.model_validate(d)
                for d in json.loads((run_dir / "concepts.json").read_text())]
    assert concepts
    loaded = eval_inputs.load("output", run_dir, "t", [])
    record = {r.kind: r for r in loaded.records}["concepts"]
    assert record.state == "loaded"
    assert loaded.concepts


def test_every_concept_is_llm_derived_and_carries_a_confidence(tmp_path, monkeypatch):
    install_llm(monkeypatch, FakeClaude(fixture_reply))
    _code, run_dir = run(tmp_path)
    for row in json.loads((run_dir / "concepts.json").read_text()):
        assert row["llm_derived"] is True
        assert 0.0 <= row["confidence"] <= 1.0
        assert row["member_node_ids"]


def test_members_are_real_nodes_from_the_trees(tmp_path, monkeypatch):
    from pipeline.vocabulary import treeio
    install_llm(monkeypatch, FakeClaude(fixture_reply))
    _code, run_dir = run(tmp_path)
    known = set(treeio.load_trees("fixtures", FIXTURES, "t").by_id())
    for row in json.loads((run_dir / "concepts.json").read_text()):
        assert set(row["member_node_ids"]) <= known


def test_the_merge_log_and_the_collisions_are_written(tmp_path, monkeypatch):
    install_llm(monkeypatch, FakeClaude(fixture_reply))
    _code, run_dir = run(tmp_path)
    resolution = json.loads((run_dir / "concepts" / "resolution.json").read_text())
    assert set(resolution) >= {"proposed", "minted", "not_minted_term_collision",
                               "merged_away", "merge_log", "collisions",
                               "resolution_method", "merge_threshold"}
    assert resolution["merge_threshold"] == config.CONCEPT_MERGE_COSINE


def test_a_label_that_is_a_declared_term_is_logged_not_minted(tmp_path, monkeypatch):
    """The fixture's Joint Schedule 1 declares `Good Working Practice`. A concept
    proposing it must lose to the term."""
    def collide(_task, prompt):
        listing = prompt.split("PROVISIONS (path :: text):\n", 1)[1]
        first = listing.split("\n", 1)[0].split(" :: ", 1)[0]
        return reply(concept("Good Working Practice", 0.9, [first]))
    install_llm(monkeypatch, FakeClaude(collide))
    _code, run_dir = run(tmp_path)
    assert json.loads((run_dir / "concepts.json").read_text()) == []
    resolution = json.loads((run_dir / "concepts" / "resolution.json").read_text())
    assert resolution["not_minted_term_collision"] > 0
    assert resolution["collisions"][0]["collides_with_term"] == "Good Working Practice"


def test_associated_term_is_not_computed_here(tmp_path, monkeypatch):
    """SPEC 2.4 puts it in stage 7, because it joins stage 4 and stage 5 output
    and these stages must not read each other."""
    install_llm(monkeypatch, FakeClaude(fixture_reply))
    _code, run_dir = run(tmp_path)
    summary = json.loads((run_dir / "concepts" / "summary.json").read_text())
    assert "stage 7" in summary["associated_term"]
    assert "ASSOCIATED_TERM" not in (run_dir / "concepts.json").read_text()


def test_without_llm_py_the_run_is_honest_about_scanning_nothing(tmp_path,
                                                                 monkeypatch):
    monkeypatch.setitem(sys.modules, "pipeline.llm", None)
    code, run_dir = run(tmp_path)
    assert code == 0
    assert json.loads((run_dir / "concepts.json").read_text()) == []
    summary = json.loads((run_dir / "concepts" / "summary.json").read_text())
    assert summary["scan"]["by_state"] == {llmio.PENDING_MODULE: summary["scan"]["units"]}
    assert summary["llm"]["available"] is False
    assert "pending llm.py" in summary["llm"]["note"]
    scan = json.loads((run_dir / "concepts" / "scan.json").read_text())
    assert all(u["prompt"] for u in scan["units"]), "prompts are built and stored"


def test_a_rerun_replays_and_calls_nothing(tmp_path, monkeypatch):
    fake = FakeClaude(fixture_reply)
    install_llm(monkeypatch, fake)
    run(tmp_path)
    calls = len(fake.prompts)
    _code, run_dir = run(tmp_path)
    assert len(fake.prompts) == calls
    summary = json.loads((run_dir / "concepts" / "summary.json").read_text())
    assert set(summary["scan"]["by_state"]) == {llmio.REPLAYED}


def test_the_scan_covers_every_unit_and_reports_the_empty_ones(tmp_path, monkeypatch):
    install_llm(monkeypatch, FakeClaude(reply()))
    _code, run_dir = run(tmp_path)
    summary = json.loads((run_dir / "concepts" / "summary.json").read_text())
    assert summary["scan"]["units"] > 0
    assert summary["scan"]["units_with_no_concept"] == summary["scan"]["units"]
