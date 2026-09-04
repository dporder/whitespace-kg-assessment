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


def test_concepts_json_carries_the_scope_inline_and_still_loads(tmp_path,
                                                                monkeypatch):
    """A reader of concepts.json alone must see that the run was sampled. The
    file has to stay a bare list, because SPEC 3 names it and the stage 8 loader
    validates every element as a Concept, so the scope rides on each record
    where the frozen model ignores it as an unknown field."""
    from pipeline.eval import inputs as eval_inputs
    install_llm(monkeypatch, FakeClaude(fixture_reply))
    code = main(["--input", "fixtures", "--fixtures-dir", str(FIXTURES),
                 "--output-dir", str(tmp_path), "--run", "t", "--quiet",
                 "--no-embed", "--parts", "core-terms"])
    assert code == 0
    raw = json.loads((tmp_path / "t" / "concepts.json").read_text())
    assert isinstance(raw, list)
    assert all(r["scanned_parts"] == ["core-terms"] for r in raw)
    assert all(set(r["skipped_parts"]) == {"award-form", "joint-schedule-1"}
               for r in raw)
    # The frozen loader still reads it, and the extra keys do not become fields.
    loaded = eval_inputs.load("output", tmp_path / "t", "t", [])
    record = {r.kind: r for r in loaded.records}["concepts"]
    assert record.state == "loaded", record.error
    assert loaded.concepts
    assert not hasattr(loaded.concepts[0], "scanned_parts")


def test_a_sampled_scan_records_which_parts_it_skipped(tmp_path, monkeypatch):
    """The scan is the pipeline's spend bottleneck, so a run may deliberately
    sample it. That makes "this part has no concepts" ambiguous between *not
    scanned* and *scanned and found nothing*, and stage 8 measures coverage over
    every loaded tree. Recording the scope is what keeps the two apart."""
    install_llm(monkeypatch, FakeClaude(fixture_reply))
    code = main(["--input", "fixtures", "--fixtures-dir", str(FIXTURES),
                 "--output-dir", str(tmp_path), "--run", "t", "--quiet",
                 "--no-embed", "--parts", "core-terms"])
    assert code == 0
    scope = json.loads((tmp_path / "t" / "concepts" / "scope.json").read_text())
    assert scope["scanned_parts"] == ["core-terms"]
    assert set(scope["skipped_parts"]) == {"award-form", "joint-schedule-1"}
    assert "never scanned" in scope["note"]
    scopes = {c["scope_path"].split("/")[0]
              for c in json.loads((tmp_path / "t" / "concepts.json").read_text())}
    assert scopes == {"core-terms"}


def test_all_is_the_same_code_path_as_a_sampled_scan(tmp_path, monkeypatch):
    """Sampling is a flag, not a fork: `--all` and a parts list run identical
    code, so the full-capability path cannot rot while the sampled one ships."""
    install_llm(monkeypatch, FakeClaude(fixture_reply))
    main(["--input", "fixtures", "--fixtures-dir", str(FIXTURES),
          "--output-dir", str(tmp_path), "--run", "all", "--quiet",
          "--no-embed", "--all"])
    every = json.loads((tmp_path / "all" / "concepts" / "scope.json").read_text())
    assert every["skipped_parts"] == []
    assert set(every["scanned_parts"]) == {"award-form", "core-terms",
                                           "joint-schedule-1"}

    main(["--input", "fixtures", "--fixtures-dir", str(FIXTURES),
          "--output-dir", str(tmp_path), "--run", "some", "--quiet",
          "--no-embed", "--parts", "core-terms", "award-form"])
    some = json.loads((tmp_path / "some" / "concepts" / "scope.json").read_text())
    assert some["skipped_parts"] == ["joint-schedule-1"]
    # The concepts a scanned part produces do not depend on what else was in
    # scope, which is what makes a sampled run a subset rather than a different
    # answer.
    def labels_for(run, part):
        return sorted(c["label"] for c in
                      json.loads((tmp_path / run / "concepts.json").read_text())
                      if c["scope_path"].split("/")[0] == part)
    assert labels_for("all", "core-terms") == labels_for("some", "core-terms")


def test_sampling_does_not_disarm_the_term_collision_guard(tmp_path, monkeypatch):
    """The declared vocabulary is document-wide, so it must be derived from every
    tree present even when the scan is sampled. Deriving it from the scanned
    parts alone silently disarmed the guard: this pack keeps all 259 declared
    terms in Joint Schedule 1, so sampling the clause parts took the real
    collision count from 14 to 0 and would have minted concepts a Term owns."""
    def collide(_task, prompt):
        listing = prompt.split("PROVISIONS (path :: text):\n", 1)[1]
        first = listing.split("\n", 1)[0].split(" :: ", 1)[0]
        return reply(concept("Good Working Practice", 0.9, [first]))
    install_llm(monkeypatch, FakeClaude(collide))
    # core-terms is scanned; the fixture's definitions schedule is not.
    code = main(["--input", "fixtures", "--fixtures-dir", str(FIXTURES),
                 "--output-dir", str(tmp_path), "--run", "t", "--quiet",
                 "--no-embed", "--parts", "core-terms"])
    assert code == 0
    scope = json.loads((tmp_path / "t" / "concepts" / "scope.json").read_text())
    assert scope["scanned_parts"] == ["core-terms"]
    assert "joint-schedule-1" in scope["vocabulary_derived_from_parts"]
    resolution = json.loads(
        (tmp_path / "t" / "concepts" / "resolution.json").read_text())
    assert resolution["not_minted_term_collision"] > 0, \
        "the guard must still see Joint Schedule 1's declared terms"
    assert json.loads((tmp_path / "t" / "concepts.json").read_text()) == []


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


def test_a_rerun_delegates_and_reports_it(tmp_path, monkeypatch):
    """pipeline.llm owns the replay cache, so a rerun is free at that layer, not
    this one, and the summary says `delegated` rather than claiming a local
    replay it did not perform."""
    fake = FakeClaude(fixture_reply)
    install_llm(monkeypatch, fake)
    run(tmp_path)
    calls = len(fake.prompts)
    _code, run_dir = run(tmp_path)
    assert len(fake.prompts) == calls * 2
    summary = json.loads((run_dir / "concepts" / "summary.json").read_text())
    assert set(summary["scan"]["by_state"]) == {llmio.DELEGATED}


def test_the_scan_covers_every_unit_and_reports_the_empty_ones(tmp_path, monkeypatch):
    install_llm(monkeypatch, FakeClaude(reply()))
    _code, run_dir = run(tmp_path)
    summary = json.loads((run_dir / "concepts" / "summary.json").read_text())
    assert summary["scan"]["units"] > 0
    assert summary["scan"]["units_with_no_concept"] == summary["scan"]["units"]
