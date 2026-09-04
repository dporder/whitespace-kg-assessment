"""Gate, plan and the bounded tool loop, with the model scripted.

The model is replaced by a script so these tests exercise OUR code: the loop's
event order, its bounds, and above all the citation check. Nothing here proves
anything about the live API, which is deliberate; a network call would make
this suite slow, flaky and untestable in CI.
"""
import json

import pytest

from chat import agent, llm_client
from chat.tools import ToolRunner


class Script:
    """Stands in for llm_client. Hands back queued turns in order."""

    def __init__(self, gate="research", plan=None, turns=()):
        self.gate_text = gate
        self.plan_obj = plan
        self.turns = list(turns)
        self.completes: list[str] = []
        self.streams = 0

    def complete(self, *, task, model, system, messages, **kw):
        self.completes.append(task)
        if task == "chat_gate":
            return llm_client.LLMResponse(text=self.gate_text, stop_reason="end_turn")
        if task == "chat_plan":
            return llm_client.LLMResponse(
                text=json.dumps(self.plan_obj or {"restated": "r", "batches": []}),
                stop_reason="end_turn",
            )
        raise AssertionError(f"unexpected complete task {task}")

    def stream(self, *, task, model, system, messages, tools=None, **kw):
        assert task == "chat_agent"
        assert tools, "the agent turn must be given its tools"
        turn = self.turns[self.streams]
        self.streams += 1
        for chunk in turn.get("text_chunks", []):
            yield ("text", chunk)
        yield (
            "done",
            llm_client.LLMResponse(
                text="".join(turn.get("text_chunks", [])),
                tool_uses=turn.get("tool_uses", []),
                stop_reason="tool_use" if turn.get("tool_uses") else "end_turn",
            ),
        )


@pytest.fixture
def scripted(monkeypatch):
    def install(script):
        monkeypatch.setattr(agent.llm_client, "complete", script.complete)
        monkeypatch.setattr(agent.llm_client, "stream", script.stream)
        return script

    return install


def collect(question, runner=None):
    return list(agent.run_turn(question, runner=runner))


# --------------------------------------------------------------------- gate
def test_gate_routes_a_plain_lookup(scripted):
    scripted(Script(gate="lookup", turns=[{"text_chunks": ["done"]}]))
    events = dict(collect("What does Clause 9.2 say?")[:1])
    assert events["gate"]["route"] == "lookup"
    assert events["gate"]["failed_open"] is False


def test_gate_fails_open_to_research_when_the_model_is_unavailable(monkeypatch):
    def boom(**kw):
        raise llm_client.LLMUnavailable("no key")

    monkeypatch.setattr(agent.llm_client, "complete", boom)
    route, reason, failed_open = agent.gate("anything at all")
    assert route == "research"
    assert failed_open is True
    assert "failed open" in reason


def test_gate_fails_open_on_an_unexpected_answer(scripted):
    scripted(Script(gate="banana", turns=[{"text_chunks": ["x"]}]))
    route, _reason, failed_open = agent.gate("anything")
    assert route == "research" and failed_open is True


# --------------------------------------------------------------------- plan
def test_plan_is_shown_with_its_parallel_batch_structure(scripted):
    plan = {
        "restated": "Who owns New IPR, and what is that subject to?",
        "batches": [
            {"n": 1, "why": "independent", "queries": [
                {"ask": "find the IPR ownership clause", "tool": "find_provision"},
                {"ask": "define Central Buying Office", "tool": "define"},
            ]},
            {"n": 2, "why": "needs the path from batch 1", "queries": [
                {"ask": "follow its references", "tool": "follow_references"},
            ]},
        ],
    }
    scripted(Script(plan=plan, turns=[{"text_chunks": ["ok"]}]))
    events = collect("Who owns New IPR?")
    payload = next(p for e, p in events if e == "plan")
    assert payload["restated"] == plan["restated"]
    assert [b["n"] for b in payload["batches"]] == [1, 2]
    assert len(payload["batches"][0]["queries"]) == 2


def test_plan_degrades_visibly_rather_than_inventing(monkeypatch):
    monkeypatch.setattr(agent.llm_client, "complete",
                        lambda **kw: (_ for _ in ()).throw(llm_client.LLMUnavailable("down")))
    p = agent.plan("q", "research")
    assert p["batches"] == []
    assert "unavailable" in p["degraded"]


def test_plan_survives_non_json(scripted):
    s = Script()
    s.plan_obj = None
    scripted(s)
    monkey = agent.llm_client.complete

    def bad(**kw):
        if kw["task"] == "chat_plan":
            return llm_client.LLMResponse(text="I think we should look at clause 9.")
        return monkey(**kw)

    agent.llm_client.complete = bad
    try:
        p = agent.plan("q", "research")
    finally:
        agent.llm_client.complete = monkey
    assert p["degraded"] == "planner returned non-JSON"


# ---------------------------------------------------------------- tool loop
FULL_TURN = [
    {
        "text_chunks": ["Looking up the ownership clause. "],
        "tool_uses": [
            {"id": "t1", "name": "find_provision", "input": {"query": "New IPR ownership"}},
            {"id": "t2", "name": "define", "input": {"term": "Central Buying Office"}},
        ],
    },
    {
        "tool_uses": [
            {"id": "t3", "name": "get_provision", "input": {"path": "core-terms/9/9.2"}},
            {"id": "t4", "name": "cite", "input": {"path": "core-terms/9/9.2"}},
        ],
    },
    {
        "text_chunks": [
            "New IPR created under a Contract is owned by the Central Buying Office ",
            "[[core-terms/9/9.2|2]], defined as the central purchasing authority ",
            "[[joint-schedule-1/2/table/2/1|1]].",
        ],
    },
]


def test_a_full_turn_streams_gate_plan_tools_text_citations_done(scripted):
    scripted(Script(turns=FULL_TURN))
    events = collect("Who owns New IPR created under a Contract?")
    order = [e for e, _ in events]

    assert order[0] == "gate"
    assert order[1] == "plan"
    assert order[-1] == "done"
    assert "citations" in order
    assert order.index("plan") < order.index("tool"), "no tool runs before the plan"

    tools_run = [p["name"] for e, p in events if e == "tool"]
    assert tools_run == ["find_provision", "define", "get_provision", "cite"]
    assert all(p["ok"] for e, p in events if e == "tool")

    done = next(p for e, p in events if e == "done")
    assert done["rounds"] == 3
    assert done["tool_calls"] == 4
    assert done["backend"] == "fixtures"


def test_every_citation_in_the_answer_resolves_through_the_fixtures_backend(scripted):
    scripted(Script(turns=FULL_TURN))
    runner = ToolRunner()
    events = collect("Who owns New IPR created under a Contract?", runner=runner)

    cites = next(p for e, p in events if e == "citations")["citations"]
    assert len(cites) == 2
    assert all(c["status"] == "ok" for c in cites), cites

    # and each one renders a real crop through the same backend
    for c in cites:
        out = runner.backend.cite(c["path"])
        assert out["found"] is True
        assert out["page"] == c["page"]
        assert out["png"][:8] == b"\x89PNG\r\n\x1a\n"


def test_an_invented_citation_is_caught(scripted):
    scripted(Script(turns=[
        {"text_chunks": ["Clause 40.7 says otherwise [[core-terms/40/40.7|9]]."]},
    ]))
    events = collect("anything")
    cites = next(p for e, p in events if e == "citations")["citations"]
    assert cites[0]["status"] == "unknown_path"
    assert cites[0]["crop_url"] is None


def test_a_real_path_with_the_wrong_page_is_caught():
    runner = ToolRunner()
    runner.run("get_provision", {"path": "core-terms/9/9.2"})
    found = agent.verify_citations("owned by CBO [[core-terms/9/9.2|41]]", runner)
    assert found[0].status == "page_mismatch"


def test_citations_are_deduplicated():
    runner = ToolRunner()
    runner.run("get_provision", {"path": "core-terms/9/9.2"})
    text = "a [[core-terms/9/9.2|2]] and again [[core-terms/9/9.2|2]]"
    assert len(agent.verify_citations(text, runner)) == 1


def test_the_loop_is_bounded(scripted, monkeypatch):
    """A model that only ever calls tools still terminates."""
    monkeypatch.setattr(agent.ui_config, "MAX_TOOL_ROUNDS", 3)
    forever = [{"tool_uses": [{"id": f"t{i}", "name": "get_provision",
                              "input": {"path": "core-terms/9/9.2"}}]} for i in range(10)]
    scripted(Script(turns=forever))
    events = collect("loop forever")
    assert sum(1 for e, _ in events if e == "tool") == 3
    assert any(e == "note" for e, _ in events)


def test_tool_budget_stops_the_loop_without_crashing(scripted, monkeypatch):
    monkeypatch.setattr(agent.ui_config, "MAX_TOOL_CALLS", 2)
    monkeypatch.setattr(agent.ui_config, "MAX_TOOL_ROUNDS", 3)
    turns = [{"tool_uses": [
        {"id": f"a{i}", "name": "get_provision", "input": {"path": "core-terms/9/9.2"}},
        {"id": f"b{i}", "name": "get_provision", "input": {"path": "core-terms/9/9.1"}},
    ]} for i in range(3)]
    scripted(Script(turns=turns))
    events = collect("spend the budget")
    assert sum(1 for e, _ in events if e == "tool") == 2


def test_the_model_never_receives_image_bytes(scripted):
    """cite() bytes must not enter the transcript."""
    scripted(Script(turns=[
        {"tool_uses": [{"id": "c1", "name": "cite", "input": {"path": "core-terms/9/9.2"}}]},
        {"text_chunks": ["done [[core-terms/9/9.2|2]]"]},
    ]))
    runner = ToolRunner()
    collect("cite it", runner=runner)
    payload = runner.result_json(runner.calls[0])
    assert "\\x89PNG" not in payload and "png" not in json.loads(payload)
    assert json.loads(payload)["byte_length"] > 1000


def test_error_is_streamed_when_the_model_dies_mid_loop(monkeypatch):
    monkeypatch.setattr(agent.llm_client, "complete",
                        lambda **kw: llm_client.LLMResponse(text="research"))

    def dead(**kw):
        raise llm_client.LLMUnavailable("upstream refused")
        yield  # pragma: no cover

    monkeypatch.setattr(agent.llm_client, "stream", dead)
    events = collect("anything")
    err = next(p for e, p in events if e == "error")
    assert "upstream refused" in err["message"]
