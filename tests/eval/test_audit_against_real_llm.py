"""The audit's checker seam against the real `pipeline/llm.py`.

The live failure was at the seam between two modules that each worked, so a
fake `pipeline.llm` cannot prove it fixed: `test_audit_checker.py` pins this
section's handling, and this file pins that the handling matches what llm.py
actually does. It skips while llm.py lives on the resolver branch and starts
running the moment it lands on master.

No API key is needed. `llm.set_client` injects a stand-in SDK client, so the
real module does the real work either side of the network: model selection from
`config.MODELS`, the payload, `parse_json`, and the `llm_log` record. That last
one matters, because the live run's log was empty for the judge and an empty
log is how nobody could tell where the call went.
"""
from __future__ import annotations

import json
import types

import pytest

llm = pytest.importorskip("pipeline.llm",
                          reason="pipeline/llm.py is not on this branch yet")

from pipeline.eval.sections.stratified_audit import LLM_TASK, _run_checker  # noqa: E402


class FakeMessage:
    """The shape llm._normalise reads: content blocks, stop reason, usage."""

    def __init__(self, text):
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.stop_reason = "end_turn"
        self.model = "stub-model"
        self.usage = types.SimpleNamespace(input_tokens=10, output_tokens=20)


class FakeSDK:
    def __init__(self, reply):
        self._reply = reply
        self.payloads: list[dict] = []
        outer = self

        class Messages:
            def create(self, **payload):
                outer.payloads.append(payload)
                body = outer._reply
                if callable(body):
                    body = body(payload)
                return FakeMessage(body)

        self.messages = Messages()


@pytest.fixture
def real_llm(tmp_path, monkeypatch):
    """The real module, pointed at a temp run dir with a stand-in client."""
    for setter, arg in (("set_run_dir", tmp_path / "run"), ("set_client", None)):
        if not hasattr(llm, setter):
            pytest.skip(f"pipeline.llm exposes no {setter}")
    llm.set_run_dir(tmp_path / "run")
    if hasattr(llm, "reset_breaker"):
        llm.reset_breaker()
    yield
    llm.set_client(None)


def items(n):
    return [{"kind": "term_use", "term": f"T{i}", "path": f"p/{i}",
             "sentence": "The Provider must supply Outputs."} for i in range(n)]


def answering(wrapper=lambda body: body):
    def reply(payload):
        prompt = payload["messages"][0]["content"]
        indices = [item["i"] for item in json.loads(prompt[prompt.index("["):])]
        return wrapper(json.dumps([{"i": i, "agree": True, "why": "ok"}
                                   for i in indices]))
    return reply


def test_the_real_seam_scores_a_clean_reply(real_llm, tmp_path):
    llm.set_client(FakeSDK(answering()))
    verdicts, note, diagnostics = _run_checker(items(3), tmp_path / "eval")

    assert verdicts is not None, note
    assert len(verdicts) == 3
    assert diagnostics["parser"] == "pipeline.llm.parse_json"


def test_the_real_seam_scores_a_prose_wrapped_reply(real_llm, tmp_path):
    """The live failure: json.loads on this raises at line 1 column 1."""
    llm.set_client(FakeSDK(answering(
        lambda body: f"Sure, here are my verdicts:\n```json\n{body}\n```")))
    verdicts, note, _diag = _run_checker(items(3), tmp_path / "eval")

    assert verdicts is not None, note
    assert len(verdicts) == 3


def test_the_real_seam_counts_an_empty_reply_and_keeps_the_raw_text(real_llm, tmp_path):
    llm.set_client(FakeSDK(""))
    verdicts, note, diagnostics = _run_checker(items(3), tmp_path / "eval")

    assert verdicts is None
    assert "nothing scorable" in note
    failed = diagnostics["batches"][0]
    assert failed["state"] == "unparseable"
    assert (tmp_path / "eval" / "audit_raw" / "batch-0.txt").exists()


def test_the_call_reaches_the_llm_log(real_llm, tmp_path):
    """The live run had nothing in output/current/llm_log for the judge, which
    is why nobody could tell whether the call had been made at all."""
    if not hasattr(llm, "log_dir"):
        pytest.skip("pipeline.llm exposes no log_dir")
    llm.set_client(FakeSDK(answering()))
    _run_checker(items(2), tmp_path / "eval")

    records = list(llm.log_dir().glob("**/*.json"))
    assert records, f"nothing logged under {llm.log_dir()}"
    body = json.loads(records[0].read_text())
    assert LLM_TASK in json.dumps(body) or LLM_TASK in records[0].name


def test_the_task_selects_the_judge_model_from_config(real_llm, tmp_path):
    import config
    sdk = FakeSDK(answering())
    llm.set_client(sdk)
    _run_checker(items(2), tmp_path / "eval")

    assert sdk.payloads, "the SDK was never called"
    assert sdk.payloads[0]["model"] == config.MODELS[LLM_TASK]


def test_a_forty_item_sample_is_batched_not_sent_as_one_call(real_llm, tmp_path):
    """llm.py defaults to 1024 max_tokens, which 40 verdicts overrun. Batching
    keeps each reply inside a budget the runner sets explicitly."""
    sdk = FakeSDK(answering())
    llm.set_client(sdk)
    verdicts, _note, diagnostics = _run_checker(items(40), tmp_path / "eval")

    assert len(sdk.payloads) == 4, "40 items at a batch size of 10"
    assert len(verdicts) == 40
    assert diagnostics["batches_failed"] == 0
    assert all(p["max_tokens"] > 1024 for p in sdk.payloads)
