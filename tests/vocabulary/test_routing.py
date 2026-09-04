"""Typed ambiguity routing: one prompt per failure mode, and the pending path.

`pipeline/llm.py` does not exist yet, so the mock here stands in for it by
injecting a module of that name. That is deliberate: the tests prove the whole
path, prompt to verdict to applied decision, so that when the real client lands
one rerun completes the work rather than a new code path being written.
"""
from __future__ import annotations

import json
import sys
import types

import config
from pipeline.vocabulary import llmio, routing
from pipeline.vocabulary.matching import Match


def make_match(kind: str, term: str = "Widget", **kw) -> Match:
    return Match(term=term, surface=kw.pop("surface", term), node_id="n1",
                 node_path="p/1/1.1", part="p", section_path="p/1",
                 field_name="text", span=(0, len(term)), status="ambiguous",
                 ambiguity_kind=kind, kinds=[kind], definition_used="document",
                 sentence=f"{term} deliveries must be logged.", **kw)


class FakeLLM:
    """Stands in for pipeline.llm. Records prompts, returns scripted replies."""

    def __init__(self, reply):
        self.prompts: list[tuple[str, str]] = []
        self._reply = reply

    def complete(self, task: str, prompt: str) -> str:
        self.prompts.append((task, prompt))
        return self._reply(task, prompt) if callable(self._reply) else self._reply


def install(monkeypatch, fake) -> None:
    module = types.ModuleType("pipeline.llm")
    module.complete = fake.complete
    monkeypatch.setitem(sys.modules, "pipeline.llm", module)


def runner(tmp_path, enabled=True) -> llmio.Runner:
    return llmio.runner("vocabulary", tmp_path / "run", tmp_path, enabled=enabled)


def no_definition(_term):
    return "the defined meaning"


def no_candidates(term):
    return [term]


# ------------------------------------------------------------- the prompts


def test_every_ambiguity_kind_has_its_own_prompt():
    """DESIGN tier 2: a model asked "is this capital a sentence start" and one
    asked "is this capital a typo" are answering different questions."""
    from pipeline.schemas import AmbiguityKind
    kinds = set(AmbiguityKind.__args__) - {"none"}
    assert set(routing.PROMPTS) == kinds
    bodies = list(routing.PROMPTS.values())
    assert len(set(bodies)) == len(bodies), "prompts must differ, not be one template"


def test_each_prompt_names_its_own_failure_mode():
    assert "start of a sentence" in routing.PROMPTS["sentence_initial"]
    assert "headings are capitalised" in routing.PROMPTS["heading"]
    assert "BOTH directions" in routing.PROMPTS["typo_dense"]
    assert "abbreviation" in routing.PROMPTS["alias_collision"]


def test_confidence_is_asked_for_before_the_verdict():
    """EVALUATION.md layer 5: the score is elicited before the answer is
    committed, so it is not a defence of a conclusion already stated."""
    item = routing.RoutedItem(index=0, match=make_match("heading"), payload={})
    prompt = routing.build_prompt("heading", [item])
    assert prompt.index('"confidence"') < prompt.index('"verdict"')
    assert "State `confidence` before `verdict`" in prompt


def test_items_are_batched_by_kind(tmp_path, monkeypatch):
    fake = FakeLLM("[]")
    install(monkeypatch, fake)
    matches = [make_match("heading"), make_match("sentence_initial"),
               make_match("typo_dense")]
    routing.route(matches, runner(tmp_path), no_definition, no_candidates)
    assert len(fake.prompts) == 3
    versions = {p.split("\n")[0] for _t, p in fake.prompts}
    assert len(versions) == 3, "each kind must get its own prompt, not one shared"


def test_the_routing_model_comes_from_its_own_config_entry():
    """config.MODELS now carries a vocabulary_routing entry, so the routed
    checks stop borrowing the reference resolver's model."""
    assert routing.TASK == "vocabulary_routing"
    assert config.MODELS[routing.TASK] == "claude-haiku-4-5"
    assert "vocabulary_routing" in routing.TASK_NOTE


# --------------------------------------------------------- pending llm.py


def test_without_llm_py_the_queue_is_built_and_marked_pending(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pipeline.llm", None)
    matches = [make_match("heading"), make_match("typo_dense")]
    r = runner(tmp_path)
    queues = routing.route(matches, r, no_definition, no_candidates)
    assert set(queues) == {"heading", "typo_dense"}
    for queue in queues.values():
        assert queue.state == llmio.PENDING_MODULE
        assert queue.batches[0]["prompt"], "the prompt is built and stored anyway"
        assert queue.verdicts == {}
    kept, rejected = routing.apply(matches, queues)
    assert len(kept) == 2 and rejected == []
    assert all(m.status == "ambiguous" and m.method == "exact_longest" for m in kept)


def test_no_llm_flag_builds_the_queue_without_calling(tmp_path, monkeypatch):
    fake = FakeLLM("[]")
    install(monkeypatch, fake)
    queues = routing.route([make_match("heading")], runner(tmp_path, enabled=False),
                           no_definition, no_candidates)
    assert fake.prompts == []
    assert queues["heading"].state == llmio.DISABLED


# ------------------------------------------------------------- the verdicts


def reply_for(verdict, confidence=0.95, term=None):
    def build(_task, prompt):
        indices = [item["i"] for item in json.loads(prompt.split("Items:\n", 1)[1])]
        return json.dumps([{"i": i, "confidence": confidence, "verdict": verdict,
                            "governing_term": term or "Widget", "why": "because"}
                           for i in indices])
    return build


def test_a_confirmed_use_becomes_confident_by_llm(tmp_path, monkeypatch):
    install(monkeypatch, FakeLLM(reply_for("use")))
    matches = [make_match("sentence_initial")]
    queues = routing.route(matches, runner(tmp_path), no_definition, no_candidates)
    kept, rejected = routing.apply(matches, queues)
    assert rejected == []
    assert kept[0].status == "confident"
    assert kept[0].ambiguity_kind == "none"
    assert kept[0].method == "llm"
    assert kept[0].to_schema().method == "llm"


def test_a_confident_not_a_use_removes_the_match_and_records_why(tmp_path, monkeypatch):
    install(monkeypatch, FakeLLM(reply_for("not_a_use", confidence=0.95)))
    matches = [make_match("sentence_initial")]
    queues = routing.route(matches, runner(tmp_path), no_definition, no_candidates)
    kept, rejected = routing.apply(matches, queues)
    assert kept == []
    assert rejected[0]["term"] == "Widget"
    assert rejected[0]["why"] == "because"


def test_an_unsure_not_a_use_is_kept_because_false_negatives_cost_more(
        tmp_path, monkeypatch):
    """EVALUATION.md section 2: a false negative hides an obligation from the
    person searching for it, so removing a match needs a confident checker."""
    install(monkeypatch, FakeLLM(reply_for("not_a_use", confidence=0.4)))
    matches = [make_match("sentence_initial")]
    queues = routing.route(matches, runner(tmp_path), no_definition, no_candidates)
    kept, rejected = routing.apply(matches, queues)
    assert len(kept) == 1 and rejected == []
    assert kept[0].status == "ambiguous"


def test_unsure_stays_ambiguous_for_the_review_queue(tmp_path, monkeypatch):
    install(monkeypatch, FakeLLM(reply_for("unsure")))
    matches = [make_match("typo_dense")]
    queues = routing.route(matches, runner(tmp_path), no_definition, no_candidates)
    kept, _rejected = routing.apply(matches, queues)
    assert kept[0].status == "ambiguous"
    assert kept[0].ambiguity_kind == "typo_dense"


def test_an_alias_collision_verdict_picks_the_governing_term(tmp_path, monkeypatch):
    install(monkeypatch, FakeLLM(reply_for("use", term="Handover Body")))
    match = make_match("alias_collision", term="Holding Body", surface="HB")
    match.collides_with = ["Handover Body", "Holding Body"]
    queues = routing.route([match], runner(tmp_path), no_definition, no_candidates)
    kept, _rejected = routing.apply([match], queues)
    assert kept[0].term == "Handover Body"


def test_a_fenced_json_reply_is_still_read(tmp_path, monkeypatch):
    """Claude Haiku 4.5 wrapped its verdicts in a ```json fence on the first
    live run and every one of them was thrown away as unparseable. A fence is a
    formatting habit, not a different answer."""
    def fenced(task, prompt):
        return "```json\n" + reply_for("use")(task, prompt) + "\n```"
    install(monkeypatch, FakeLLM(fenced))
    matches = [make_match("typo_dense")]
    queues = routing.route(matches, runner(tmp_path), no_definition, no_candidates)
    assert "parse_error" not in queues["typo_dense"].batches[0]
    kept, _rejected = routing.apply(matches, queues)
    assert kept[0].status == "confident" and kept[0].method == "llm"


def test_strip_fence_leaves_bare_json_alone():
    assert llmio.strip_fence('[{"i": 0}]') == '[{"i": 0}]'
    assert llmio.strip_fence('```json\n[{"i": 0}]\n```') == '[{"i": 0}]'
    assert llmio.strip_fence('```\n{"a": 1}\n```') == '{"a": 1}'
    assert llmio.strip_fence("not json") == "not json"


def test_a_batch_whose_reply_would_not_parse_is_not_reported_as_checked(
        tmp_path, monkeypatch):
    """Calling it "checked" would claim an agreement nobody measured."""
    install(monkeypatch, FakeLLM("still not json"))
    queues = routing.route([make_match("heading")], runner(tmp_path),
                           no_definition, no_candidates)
    assert queues["heading"].state == "checked_with_parse_errors"
    assert "unparseable" in queues["heading"].note


def test_an_unparseable_reply_fails_the_batch_not_the_run(tmp_path, monkeypatch):
    install(monkeypatch, FakeLLM("not json at all"))
    matches = [make_match("heading")]
    queues = routing.route(matches, runner(tmp_path), no_definition, no_candidates)
    assert "parse_error" in queues["heading"].batches[0]
    kept, rejected = routing.apply(matches, queues)
    assert len(kept) == 1 and kept[0].status == "ambiguous" and rejected == []


# --------------------------------------------------------------- the cache


def test_every_call_is_delegated_when_pipeline_llm_is_present(tmp_path, monkeypatch):
    """SPEC's one-LLM-path rule: when pipeline.llm exists it owns the call and
    its own replay cache, so this seam neither shadows nor second-guesses it.
    Consulting a local cache first would let a rerun be served by whichever
    cache answered, and the log would stop describing what actually ran."""
    fake = FakeLLM(reply_for("use"))
    install(monkeypatch, fake)
    routing.route([make_match("heading")], runner(tmp_path), no_definition,
                  no_candidates)
    assert len(fake.prompts) == 1
    again = [make_match("heading")]
    queues = routing.route(again, runner(tmp_path), no_definition, no_candidates)
    assert len(fake.prompts) == 2, "the call is delegated, not served locally"
    assert queues["heading"].batches[0]["call"]["state"] == llmio.DELEGATED
    kept, _r = routing.apply(again, queues)
    assert kept[0].status == "confident"


def test_the_local_cache_serves_only_while_pipeline_llm_is_absent(tmp_path,
                                                                  monkeypatch):
    """The documented fallback for the window before pipeline/llm.py lands."""
    install(monkeypatch, FakeLLM("[]"))
    r = runner(tmp_path)
    call = r.complete("vocabulary_routing", "v1", "a prompt")
    assert call.state == llmio.DELEGATED
    r.write_cache(call)                       # what the cache warmer does

    monkeypatch.setitem(sys.modules, "pipeline.llm", None)
    replayed = runner(tmp_path).complete("vocabulary_routing", "v1", "a prompt")
    assert replayed.state == llmio.REPLAYED
    assert replayed.response == call.response
    assert "fallback" in replayed.note

    missing = runner(tmp_path).complete("vocabulary_routing", "v1", "another")
    assert missing.state == llmio.PENDING_MODULE


def test_the_call_log_records_model_prompt_version_and_response(tmp_path, monkeypatch):
    install(monkeypatch, FakeLLM(reply_for("use")))
    r = runner(tmp_path)
    routing.route([make_match("heading")], r, no_definition, no_candidates)
    path = r.flush_log()
    row = json.loads(path.read_text().strip())
    assert row["stage"] == "vocabulary"
    assert row["model"] == config.MODELS[routing.TASK]
    assert row["prompt_version"].startswith(routing.PROMPT_VERSION)
    assert row["response"]


def test_a_credential_refusal_is_marked_pending_not_failed(tmp_path, monkeypatch):
    class Refusing:
        def complete(self, task, prompt):
            raise RuntimeError("401 authentication_error: invalid x-api-key")
    install(monkeypatch, Refusing())
    queues = routing.route([make_match("heading")], runner(tmp_path),
                           no_definition, no_candidates)
    assert queues["heading"].state == llmio.PENDING_CREDENTIALS
