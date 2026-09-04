"""Stage 6 end to end: `python -m pipeline.embeddings` over the fixtures."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import config
from pipeline.schemas import EmbeddingRecord
from pipeline.embeddings.__main__ import main
from pipeline.vocabulary import llmio
from tests.vocabulary.llm_seam import install_llm as _install
from tests.vocabulary.llm_seam import without_llm

FIXTURES = config.ROOT / "fixtures"


def run(tmp_path: Path, *extra: str) -> tuple[int, Path]:
    code = main(["--input", "fixtures", "--fixtures-dir", str(FIXTURES),
                 "--output-dir", str(tmp_path), "--run", "t", "--quiet", *extra])
    return code, tmp_path / "t" / "embeddings"


def install_llm(monkeypatch, reply="A short generated summary of the provision."):
    """Both places Python looks; see tests/vocabulary/llm_seam.py."""
    _install(monkeypatch, lambda task, prompt, **kw: reply)


def test_the_cli_runs_clean_and_writes_its_files(tmp_path, fake_openai):
    code, out = run(tmp_path)
    assert code == 0
    for name in ("plan.json", "records.json", "index.json", "pending.json",
                 "summary.json"):
        assert (out / name).exists(), name
    assert not (out / "violations.json").exists()


def test_records_validate_and_carry_a_vector_that_exists(tmp_path, fake_openai):
    _code, out = run(tmp_path)
    records = [EmbeddingRecord.model_validate(d)
               for d in json.loads((out / "records.json").read_text())]
    assert records
    for record in records:
        assert (tmp_path / record.vector_ref).exists()
        assert record.level in ("leaf_text", "leaf_window", "subtree_text", "summary")


def test_the_index_is_keyed_by_node_id_and_the_vectors_are_not_on_nodes(
        tmp_path, fake_openai):
    _code, out = run(tmp_path)
    index = json.loads((out / "index.json").read_text())
    assert index["keyed_by"] == "node_id"
    for node_id, entry in index["entries"].items():
        assert len(node_id) == 40                    # sha1 node id
        assert entry["vector_ref"].startswith("embeddings_cache/")
    trees = json.loads((FIXTURES / "tree" / "core-terms.json").read_text())
    assert "vector" not in json.dumps(trees)


def test_a_summary_with_no_model_is_pending_and_mints_no_record(tmp_path,
                                                                fake_openai,
                                                                monkeypatch):
    """The honest shape when pipeline/llm.py is absent: the item is queued with
    its reason, and no EmbeddingRecord claims a summary that was never written."""
    without_llm(monkeypatch)
    _code, out = run(tmp_path)
    pending = json.loads((out / "pending.json").read_text())
    owed = [p for p in pending["items"] if p["blocked_on"] == "summary"]
    assert owed, "parts and documents always owe a summary"
    assert all(p["state"] == llmio.PENDING_MODULE for p in owed)
    records = json.loads((out / "records.json").read_text())
    assert not [r for r in records if r["level"] == "summary"]


def test_with_a_model_the_summaries_are_generated_and_flagged_llm_derived(
        tmp_path, fake_openai, monkeypatch):
    install_llm(monkeypatch)
    code, out = run(tmp_path)
    assert code == 0
    records = json.loads((out / "records.json").read_text())
    summaries = [r for r in records if r["level"] == "summary"]
    assert summaries, "parts owe summaries and the model supplied them"
    assert all(r["llm_derived"] is True for r in summaries)
    assert all(r["text"] == "A short generated summary of the provision."
               for r in summaries)
    assert not json.loads((out / "pending.json").read_text())["items"]


def test_a_generated_summary_never_becomes_a_node_text(tmp_path, fake_openai,
                                                       monkeypatch):
    """A summary is generated text and a hit on it must resolve to a citable
    leaf before anything is quoted, so it never enters the tree."""
    install_llm(monkeypatch)
    run(tmp_path)
    before = (FIXTURES / "tree" / "core-terms.json").read_text()
    assert "A short generated summary" not in before


def test_the_summary_prompt_is_compression_not_advice(tmp_path):
    from pipeline.embeddings import summaries
    assert summaries.TASK == "summaries"
    assert config.MODELS[summaries.TASK] == "claude-haiku-4-5"
    assert "do not add anything the text does not say" in summaries.PROMPT
    assert "Keep every capitalised defined term exactly as written" in summaries.PROMPT


def test_a_rerun_costs_nothing_for_vectors(tmp_path, fake_openai, monkeypatch):
    """The embedding store is this stage's own content-addressed cache, so a
    rerun re-embeds nothing. Summaries go through pipeline.llm, which owns its
    replay cache, so those are delegated again and reported as such."""
    install_llm(monkeypatch)
    run(tmp_path)
    calls_before = sum(len(i.calls) for i in fake_openai.instances)
    _code, out = run(tmp_path)
    assert sum(len(i.calls) for i in fake_openai.instances) == calls_before
    summary = json.loads((out / "summary.json").read_text())
    assert summary["vectors"]["api_calls"] == 0
    assert summary["vectors"]["newly_embedded"] == 0
    assert summary["llm"]["by_state"] == {"delegated": summary["summaries"]["owed"]}


def test_the_leaf_window_flag_replaces_leaf_text(tmp_path, fake_openai):
    _code, out = run(tmp_path, "--leaf-window")
    levels = {r["level"] for r in json.loads((out / "records.json").read_text())}
    assert "leaf_window" in levels and "leaf_text" not in levels
    _code, out = run(tmp_path, "--no-leaf-window")
    levels = {r["level"] for r in json.loads((out / "records.json").read_text())}
    assert "leaf_text" in levels and "leaf_window" not in levels


def test_no_embed_plans_everything_and_calls_nothing(tmp_path, fake_openai):
    code, out = run(tmp_path, "--no-embed")
    assert code == 0
    plan = json.loads((out / "plan.json").read_text())
    assert plan["items"], "the plan is deterministic and runs without a key"
    assert json.loads((out / "records.json").read_text()) == []
    pending = json.loads((out / "pending.json").read_text())
    assert pending["count"] == len(plan["items"])
    assert fake_openai.instances == []


def test_the_plan_is_byte_identical_on_a_cold_rerun(tmp_path, fake_openai):
    import shutil
    _code, out = run(tmp_path, "--no-embed")
    before = (out / "plan.json").read_bytes()
    shutil.rmtree(tmp_path / "t")
    run(tmp_path, "--no-embed")
    assert (out / "plan.json").read_bytes() == before
