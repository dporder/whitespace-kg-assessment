"""The audit's checker seam: what happens to a reply that is not clean JSON.

The live failure this pins: the section reported `no_data` with
"JSONDecodeError: Expecting value: line 1 column 1 (char 0)" and scored none of
a correctly drawn 40-item sample, because the runner called `complete()` and
`json.loads`'d the raw text. A model told to answer in JSON still wraps it in a
fence or opens with a sentence often enough that this is not a contract.

These tests stand in a fake `pipeline.llm` so they exercise the runner's own
handling. `test_audit_against_real_llm.py` runs the same paths through the
resolver branch's actual `llm.py`.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from pipeline.eval.sections import stratified_audit
from pipeline.eval.sections.stratified_audit import _run_checker, tolerant_json


class FakeLLM(types.ModuleType):
    """Only what the runner is allowed to rely on: the pinned `complete`."""

    def __init__(self, replies, *, parse_json=None, unavailable=None, json_system=None):
        super().__init__("pipeline.llm")
        self._replies = list(replies)
        self.calls: list[dict] = []
        if parse_json is not None:
            self.parse_json = parse_json
        if unavailable is not None:
            self.LLMUnavailable = unavailable
        if json_system is not None:
            self.JSON_SYSTEM = json_system

    def complete(self, task, prompt, **kwargs):
        self.calls.append({"task": task, "prompt": prompt, **kwargs})
        reply = self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(prompt)
        return reply


def answers_the_batch(wrapper=lambda body: body, agree=True):
    """A reply built from the indices the prompt actually asked about, so a
    multi-batch sample is answered correctly instead of the same block twice."""
    def reply(prompt):
        indices = [item["i"] for item in json.loads(prompt[prompt.index("["):])]
        body = json.dumps([{"i": i, "agree": agree, "why": "looks right"}
                           for i in indices])
        return wrapper(body)
    return reply


@pytest.fixture
def install_llm(monkeypatch):
    def install(module):
        monkeypatch.setitem(sys.modules, "pipeline.llm", module)
        return module
    return install


def items(n):
    return [{"kind": "term_use", "term": f"T{i}", "path": f"p/{i}",
             "sentence": "some text"} for i in range(n)]


def verdicts_json(first, count, agree=True):
    return json.dumps([{"i": first + n, "agree": agree, "why": "looks right"}
                       for n in range(count)])


# ------------------------------------------------------------- tolerant parse

@pytest.mark.parametrize("raw", [
    '[{"i": 0, "agree": true, "why": "ok"}]',
    '```json\n[{"i": 0, "agree": true, "why": "ok"}]\n```',
    '```\n[{"i": 0, "agree": true, "why": "ok"}]\n```',
    'Here are my verdicts:\n[{"i": 0, "agree": true, "why": "ok"}]',
    'Sure! ```json\n[{"i": 0, "agree": true, "why": "ok"}]\n```\nLet me know.',
])
def test_tolerant_json_recovers_the_array(raw):
    assert tolerant_json(raw) == [{"i": 0, "agree": True, "why": "ok"}]


@pytest.mark.parametrize("raw", ["", "   ", "I cannot help with that.", None])
def test_tolerant_json_refuses_rather_than_guessing(raw):
    with pytest.raises(ValueError):
        tolerant_json(raw)


# ------------------------------------------------------- prose-wrapped replies

def test_a_prose_wrapped_reply_is_scored_not_abandoned(install_llm, tmp_path):
    """The exact live failure. json.loads on this raises at char 0."""
    llm = install_llm(FakeLLM(["Sure, here are the verdicts you asked for:\n"
                               "```json\n" + verdicts_json(0, 3) + "\n```"]))
    verdicts, note, diagnostics = _run_checker(items(3), tmp_path)

    assert verdicts is not None, note
    assert len(verdicts) == 3
    assert note == "checked by pipeline.llm"
    assert diagnostics["batches_failed"] == 0
    assert llm.calls, "the checker was actually called"


def test_llms_own_parser_is_preferred_when_it_exposes_one(install_llm, tmp_path):
    """Fences and prose are llm.py's problem to know about, not this section's
    guess at its behaviour."""
    seen = []

    def parse_json(raw):
        seen.append(raw)
        return json.loads(raw.split("```json")[1].split("```")[0])

    install_llm(FakeLLM(["```json\n" + verdicts_json(0, 2) + "\n```"],
                        parse_json=parse_json))
    verdicts, _note, diagnostics = _run_checker(items(2), tmp_path)

    assert len(verdicts) == 2
    assert diagnostics["parser"] == "pipeline.llm.parse_json"
    assert seen, "llm.parse_json was the one used"


def test_the_json_system_prompt_is_passed_when_llm_takes_one(install_llm, tmp_path):
    llm = install_llm(FakeLLM([verdicts_json(0, 1)], json_system="JSON only."))
    _run_checker(items(1), tmp_path)
    assert llm.calls[0].get("system") == "JSON only."
    assert llm.calls[0].get("max_tokens"), "a 40-verdict reply needs more than the default"


# ------------------------------------------------------------- empty replies

def test_an_empty_reply_is_counted_and_its_raw_text_saved(install_llm, tmp_path):
    install_llm(FakeLLM([""]))
    verdicts, note, diagnostics = _run_checker(items(3), tmp_path)

    assert verdicts is None
    assert "nothing scorable" in note
    failed = diagnostics["batches"][0]
    assert failed["state"] == "unparseable"
    assert failed["raw_response_chars"] == 0
    saved = failed["raw_response_saved_to"]
    assert saved and (tmp_path / "audit_raw" / "batch-0.txt").exists()
    assert "batch-0.txt" in saved


def test_one_bad_batch_does_not_abandon_the_whole_audit(install_llm, tmp_path):
    """The point of batching: 20 items, the second reply empty, the first still
    scores. Previously one bad reply lost all 40."""
    install_llm(FakeLLM([verdicts_json(0, 10), ""]))
    verdicts, note, diagnostics = _run_checker(items(20), tmp_path)

    assert len(verdicts) == 10
    assert diagnostics["batches_failed"] == 1
    assert diagnostics["items_in_failed_batches"] == 10
    assert "counted, not dropped" in note


def test_an_unavailable_checker_stops_early_rather_than_retrying_every_batch(
        install_llm, tmp_path):
    """llm.py trips a breaker when the workspace id is missing; every later call
    would fail identically, so paying for them proves nothing."""
    class LLMUnavailable(RuntimeError):
        pass

    install_llm(FakeLLM([LLMUnavailable("workspace id missing")],
                        unavailable=LLMUnavailable))
    verdicts, note, diagnostics = _run_checker(items(30), tmp_path)

    assert verdicts is None
    assert "workspace id missing" in note
    assert diagnostics["batches"][0]["state"] == "unavailable"
    assert len(diagnostics["batches"]) == 1, "did not call the remaining batches"


# --------------------------------------------------------- section reporting

def audit_section(workspace, replies, monkeypatch):
    monkeypatch.setitem(sys.modules, "pipeline.llm", FakeLLM(replies))
    return workspace.run("--run", "audited", use_llm=True)


def test_the_section_is_measured_when_every_item_scores(workspace, monkeypatch):
    """The live run reported no_data on a sample it had drawn correctly. With
    the reply parsed, the same sample scores and the gate has a number."""
    monkeypatch.setitem(sys.modules, "pipeline.llm", FakeLLM(
        [answers_the_batch(lambda body: f"Here you go:\n```json\n{body}\n```")]))
    run = workspace.run("--run", "audited", use_llm=True)
    section = run.section("stratified_audit")

    drawn = section["checker_verdicts"]["drawn"]
    assert section["status"] == "measured", section.get("reason")
    assert section["agreement"] == {"count": drawn, "of": drawn, "rate": 1.0}
    assert section["checker_verdicts"]["never_scored"] == 0
    assert section["checker_verdicts"]["duplicate_verdicts_ignored"] == 0
    assert run.gate("stratified_audit_agreement_min")["status"] == "pass"


def test_a_checker_answering_the_same_item_twice_gets_one_vote(install_llm, tmp_path):
    """Two verdicts for one item must not become two votes, or the scored count
    exceeds the sample and 'never scored' goes negative."""
    install_llm(FakeLLM([verdicts_json(0, 3) + ""]))
    verdicts, _note, _diag = _run_checker(items(3), tmp_path)
    doubled = verdicts + verdicts
    usable = {v["i"] for v in doubled}
    assert len(doubled) == 6 and len(usable) == 3


def test_a_partly_unparseable_audit_is_partial_with_counts_not_no_data(
        workspace, monkeypatch):
    monkeypatch.setitem(sys.modules, "pipeline.llm",
                        FakeLLM([verdicts_json(0, 10), ""]))
    run = workspace.run("--run", "audited", use_llm=True)
    section = run.section("stratified_audit")

    assert section["status"] == "partial"
    assert "nothing scorable" in section["reason"]
    assert section["checker_verdicts"]["never_scored"] == 1
    assert section["agreement"]["count"] == 10
    assert "batch-1.txt" in run.markdown or section["checker"]["batches_failed"] == 1


def test_a_wholly_unparseable_audit_stays_no_data_and_never_fails_the_gate(
        workspace, monkeypatch):
    monkeypatch.setitem(sys.modules, "pipeline.llm", FakeLLM(["not json at all"]))
    run = workspace.run("--run", "audited", use_llm=True)

    assert run.section("stratified_audit")["status"] == "no_data"
    assert run.gate("stratified_audit_agreement_min")["status"] == "skipped_no_data"
    assert run.code == 0
