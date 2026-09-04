"""CHAT_SCRIPTED=1, the runtime demo mode.

It exists because the API key here is identity-linked and the workspace id it
needs is unavailable, so a live exchange cannot run. The point of these tests is
that the mode is off unless asked for, loudly labelled when on, and that it
drives the REAL gate, planner, tool loop and citation checker.
"""
import pytest

from chat import agent, llm_client, scripted
from chat.tools import ToolRunner


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(scripted.SCRIPTED_ENV, "1")


def test_off_by_default(monkeypatch):
    monkeypatch.delenv(scripted.SCRIPTED_ENV, raising=False)
    assert scripted.enabled() is False
    assert "scripted" not in llm_client.backend_name()


def test_only_explicit_opt_in_turns_it_on(monkeypatch):
    for value in ("", "0", "no", "off", "false"):
        monkeypatch.setenv(scripted.SCRIPTED_ENV, value)
        assert scripted.enabled() is False, f"{value!r} must not enable it"
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv(scripted.SCRIPTED_ENV, value)
        assert scripted.enabled() is True


def test_it_is_labelled_everywhere_it_shows(on):
    assert scripted.enabled() is True
    assert llm_client.backend_name() == "scripted (demo, no model call)"
    info = agent.describe_backend()
    assert info["scripted"] is True
    assert "scripted" in info["llm"]


def test_a_full_scripted_turn_runs_the_real_loop(on):
    runner = ToolRunner()
    events = list(agent.run_turn("Who owns New IPR created under a Contract?", runner=runner))
    kinds = [e for e, _ in events]

    assert kinds[0] == "gate" and kinds[1] == "plan"
    assert kinds[-1] == "done"

    gate = next(p for e, p in events if e == "gate")
    assert gate["route"] == "research" and gate["failed_open"] is False

    plan = next(p for e, p in events if e == "plan")
    assert [b["n"] for b in plan["batches"]] == [1, 2, 3], "the parallel batch structure is real"
    assert len(plan["batches"][0]["queries"]) == 2, "batch 1 runs two calls at once"
    assert "degraded" not in plan

    tools = [p["name"] for e, p in events if e == "tool"]
    assert tools == ["find_provision", "define", "get_provision",
                     "follow_references", "cite", "get_provision", "get_provision"]
    assert all(p["ok"] for e, p in events if e == "tool")


def test_scripted_citations_are_checked_not_trusted(on):
    """The canned answer's citations still go through the real ledger."""
    runner = ToolRunner()
    events = list(agent.run_turn("Who owns New IPR created under a Contract?", runner=runner))
    cites = next(p for e, p in events if e == "citations")["citations"]
    assert len(cites) >= 4
    assert all(c["status"] == "ok" for c in cites), [c for c in cites if c["status"] != "ok"]
    for c in cites:
        out = runner.backend.cite(c["path"])
        assert out["found"] and out["page"] == c["page"]


def test_the_lookup_script_routes_through_the_gate(on):
    events = list(agent.run_turn("What does Clause 9.2 say?"))
    assert next(p for e, p in events if e == "gate")["route"] == "lookup"
    assert len(next(p for e, p in events if e == "plan")["batches"]) == 1


def test_an_unscripted_question_says_so_rather_than_inventing(on):
    events = list(agent.run_turn("What is the capital of France?"))
    answer = "".join(p["delta"] for e, p in events if e == "text")
    assert "no answer is scripted" in answer
    assert next(p for e, p in events if e == "citations")["citations"] == []
    assert next(p for e, p in events if e == "done")["uncited_claims_possible"] is True


def test_rounds_are_derived_from_the_transcript_not_shared_state(on):
    """Two interleaved turns must not consume each other's script steps."""
    a = agent.run_turn("Who owns New IPR created under a Contract?")
    b = agent.run_turn("Who owns New IPR created under a Contract?")
    next(a); next(a)                       # gate, plan
    next(b); next(b)
    tools_a = [p["name"] for e, p in list(a) if e == "tool"]
    tools_b = [p["name"] for e, p in list(b) if e == "tool"]
    assert tools_a == tools_b
