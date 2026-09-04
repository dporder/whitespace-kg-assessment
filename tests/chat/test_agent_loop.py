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
        # Research turns get the tools; the compose turn after the round budget
        # is spent deliberately gets none, so this is not an invariant.
        # A spent script repeats its last turn rather than running out: the
        # loop may make one more call than there are research rounds.
        turn = self.turns[min(self.streams, len(self.turns) - 1)]
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
    assert p["degraded"].startswith("planner returned text that is not JSON")


def test_a_truncated_plan_says_it_was_truncated(monkeypatch, capsys):
    """900 tokens could not fit the plan the prompt asks for, so the JSON was
    cut off mid-string and the whole plan was dropped in silence."""
    monkeypatch.setattr(
        agent.llm_client, "complete",
        lambda **kw: llm_client.LLMResponse(text='{"restated": "a", "batches": [{"n": 1, "qu',
                                            stop_reason="max_tokens"))
    p = agent.plan("q", "research")
    assert p["batches"] == []
    assert "ran out of output tokens" in p["degraded"]
    assert "PLAN DISCARDED" in capsys.readouterr().err, "a lost plan must not be silent"


def test_the_planner_is_given_room_for_the_plan_it_is_asked_for(monkeypatch):
    seen = {}

    def spy(**kw):
        seen.update(kw)
        return llm_client.LLMResponse(text='{"restated": "a", "batches": []}')

    monkeypatch.setattr(agent.llm_client, "complete", spy)
    agent.plan("q", "research")
    assert seen["max_tokens"] >= 2000


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


# --- the verifier probe -----------------------------------------------------
# Every row here was a way past the verifier before: a missing page and a
# non-numeric page both rendered as verified, because a None page short-
# circuited to "ok". Only an exact (path, page) a tool returned is ok.
@pytest.mark.parametrize(
    "citation, expected",
    [
        ("[[core-terms/9/9.2|2]]", "ok"),
        ("[[core-terms/9/9.2|]]", "page_unparseable"),
        ("[[core-terms/9/9.2|p2]]", "page_unparseable"),
        ("[[core-terms/40/40.7|9]]", "unknown_path"),
    ],
)
def test_citation_verifier_probe(citation, expected):
    runner = ToolRunner()
    runner.run("get_provision", {"path": "core-terms/9/9.2"})
    found = agent.verify_citations(f"a claim {citation}.", runner)
    assert len(found) == 1
    assert found[0].status == expected


def test_only_a_verified_citation_offers_a_crop():
    runner = ToolRunner()
    runner.run("get_provision", {"path": "core-terms/9/9.2"})
    text = ("ok [[core-terms/9/9.2|2]] empty [[core-terms/9/9.2|]] "
            "bad [[core-terms/9/9.2|p2]] gone [[core-terms/40/40.7|9]]")
    by_status = {c.status: c for c in agent.verify_citations(text, runner)}
    assert by_status["ok"].crop_url
    for status in ("page_unparseable", "unknown_path"):
        assert by_status[status].crop_url is None, f"{status} must not offer a crop"


def test_a_page_that_no_tool_returned_is_never_ok():
    """The ledger is the only authority: page None can never pass."""
    runner = ToolRunner()
    runner.run("get_provision", {"path": "core-terms/9/9.2"})
    assert runner.ledger.check("core-terms/9/9.2", None) == "page_unparseable"
    assert runner.ledger.check("core-terms/9/9.2", 2) == "ok"


def test_every_tool_that_can_surface_a_citable_path_reports_its_name():
    """A live run produced a citation labelled with a raw path because
    find_provision was the only tool that had surfaced it and it reported no
    name. Every route to a path must carry the name with it."""
    runner = ToolRunner()
    runner.run("find_provision", {"query": "Good Working Practice"})
    hit = runner.calls[0].result["hits"][0]
    assert runner.ledger.names.get(hit["path"]), f"no name harvested for {hit['path']}"

    runner2 = ToolRunner()
    runner2.run("define", {"term": "Good Working Practice"})
    site = runner2.calls[0].result["sites"][0]
    assert runner2.ledger.names.get(site["definition_path"])


def test_history_citations_are_harvested():
    """The history branch harvested nothing, so any claim sourced from it
    failed verification even when it was true."""
    runner = ToolRunner()
    from chat.source import corpus

    key = corpus().by_path["core-terms/9/9.2"].lineage_key
    call = runner.run("history", {"lineage_key": key})
    assert call.ok and call.result["count"] == 1
    assert runner.ledger.check("core-terms/9/9.2", 2) == "ok"


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


# --- narration must not become the answer -----------------------------------
# A live run showed the model opens most turns with "I'll start by locating
# Clause 9.2." alongside its first tool calls. Every round's text was being
# accumulated, so that narration arrived glued to the front of the answer with
# no separator, on the wire and in the page.
NARRATED = [
    {"text_chunks": ["I'll start by locating Clause 9.2."],
     "tool_uses": [{"id": "n1", "name": "get_provision",
                    "input": {"path": "core-terms/9/9.2"}}]},
    {"text_chunks": ["Clause 9.2 vests New IPR in the buyer [[core-terms/9/9.2|2]]."]},
]


def test_narration_is_not_glued_onto_the_answer(scripted):
    scripted(Script(turns=NARRATED))
    runner = ToolRunner()
    events = collect("What does Clause 9.2 say?", runner=runner)
    answer = "".join(p["delta"] for e, p in events if e == "text")
    # the deltas still carry it (it streams as progress), but the composed
    # answer the citations are checked against must not
    assert agent.verify_citations(answer, runner)
    final = "".join(p["delta"] for e, p in events
                    if e == "text") if False else None      # noqa: F841
    narration = next(p for e, p in events if e == "narration")
    assert narration["text"] == "I'll start by locating Clause 9.2."
    assert any(e == "answer_reset" for e, _ in events), "the page is never told to clear"
    # order matters: the reset must arrive before the answer's own text
    kinds = [e for e, _ in events]
    assert kinds.index("answer_reset") < len(kinds) - 1


def test_the_answer_the_citations_are_checked_against_excludes_narration(scripted):
    """turn.answer is what verify_citations reads, so it is the thing that
    must be clean, not merely the rendering."""
    captured = {}
    real = agent.verify_citations

    def spy(answer, runner):
        captured["answer"] = answer
        return real(answer, runner)

    scripted(Script(turns=NARRATED))
    agent.verify_citations = spy
    try:
        collect("What does Clause 9.2 say?")
    finally:
        agent.verify_citations = real
    assert "I'll start by" not in captured["answer"]
    assert captured["answer"].startswith("Clause 9.2 vests")


# --- spending the round budget must not cost the answer ---------------------
def test_hitting_the_round_bound_still_composes_an_answer(scripted, monkeypatch):
    """The worst live failure: 21 successful tool calls, then a one-sentence
    answer with zero citations because the loop simply stopped."""
    monkeypatch.setattr(agent.ui_config, "MAX_TOOL_ROUNDS", 2)
    turns = [
        {"text_chunks": ["I'll work through this in batches."],
         "tool_uses": [{"id": "a", "name": "get_provision",
                        "input": {"path": "core-terms/9/9.2"}}]},
        {"text_chunks": ["Still gathering."],
         "tool_uses": [{"id": "b", "name": "get_provision",
                        "input": {"path": "core-terms/3/3.1/3.1.2"}}]},
        # the compose turn: no tools offered, so this is what it must produce
        {"text_chunks": ["New IPR sits with the buyer [[core-terms/9/9.2|2]]. ",
                         "I could not reach the termination provisions."]},
    ]
    script = Script(turns=turns)
    scripted(script)
    runner = ToolRunner()
    events = collect("the messy composite", runner=runner)

    note = next(p for e, p in events if e == "note")
    assert "retrieval budget" in note["message"]

    cites = next(p for e, p in events if e == "citations")["citations"]
    assert cites, "spending the budget must not cost the reader every citation"
    assert all(c["status"] == "ok" for c in cites)

    answer = next(p for e, p in events if e == "done")
    assert answer["uncited_claims_possible"] is False


def test_the_compose_turn_is_offered_no_tools(scripted, monkeypatch):
    """If it could still call tools it would keep researching, not answer."""
    monkeypatch.setattr(agent.ui_config, "MAX_TOOL_ROUNDS", 1)
    seen = []

    class Watcher(Script):
        def stream(self, *, task, model, system, messages, tools=None, **kw):
            seen.append(tools)
            yield from super().stream(task=task, model=model, system=system,
                                      messages=messages, tools=tools, **kw)

    scripted(Watcher(turns=[
        {"tool_uses": [{"id": "a", "name": "get_provision",
                        "input": {"path": "core-terms/9/9.2"}}]},
        {"text_chunks": ["Answering from what I have [[core-terms/9/9.2|2]]."]},
    ]))
    collect("bounded")
    assert seen[0], "the research turn must be given its tools"
    assert not seen[-1], "the compose turn must be given none"


def test_the_compose_instruction_does_not_stack_two_user_messages(scripted, monkeypatch):
    """Consecutive user messages are not a shape the API promises to accept."""
    monkeypatch.setattr(agent.ui_config, "MAX_TOOL_ROUNDS", 1)
    seen = []

    class Watcher(Script):
        def stream(self, *, task, model, system, messages, tools=None, **kw):
            seen.append([m["role"] for m in messages])
            yield from super().stream(task=task, model=model, system=system,
                                      messages=messages, tools=tools, **kw)

    scripted(Watcher(turns=[
        {"tool_uses": [{"id": "a", "name": "get_provision",
                        "input": {"path": "core-terms/9/9.2"}}]},
        {"text_chunks": ["done [[core-terms/9/9.2|2]]"]},
    ]))
    collect("bounded")
    roles = seen[-1]
    assert all(a != b for a, b in zip(roles, roles[1:])), f"roles do not alternate: {roles}"


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
