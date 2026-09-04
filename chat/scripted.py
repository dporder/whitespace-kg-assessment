"""Scripted-model mode, for demonstrating the chat when no model can be called.

Opt in with `CHAT_SCRIPTED=1`. The gate, the planner and the agent loop are the
real ones; only the model's turns are canned. Every tool call, every citation
check and every crop below is the live code path over the fixtures backend, so
what the demo shows is the system working, not a picture of it working.

It is loudly labelled: /api/health reports `scripted: true` and the page shows a
"scripted model" badge. Nothing enables it implicitly.

Why it exists: the API key in this environment is identity-linked and the
workspace id it needs is not available, so a live exchange cannot run here. This
keeps the demo honest rather than absent.
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterator

SCRIPTED_ENV = "CHAT_SCRIPTED"


def enabled() -> bool:
    return os.environ.get(SCRIPTED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# the scripts
# --------------------------------------------------------------------------
IPR = {
    "match": ("ipr", "own", "new ipr"),
    "gate": "research",
    "plan": {
        "restated": "Who owns New IPR created under a Contract, and what is that ownership subject to?",
        "batches": [
            {"n": 1, "why": "independent lookups, nothing depends on the other",
             "queries": [
                 {"ask": "find the clause on ownership of New IPR", "tool": "find_provision"},
                 {"ask": "define Central Buying Office", "tool": "define"},
             ]},
            {"n": 2, "why": "needs the path batch 1 finds",
             "queries": [
                 {"ask": "read that clause in full", "tool": "get_provision"},
                 {"ask": "follow its outbound references", "tool": "follow_references"},
                 {"ask": "render its page crop", "tool": "cite"},
             ]},
            {"n": 3, "why": "needs the targets batch 2 resolves",
             "queries": [
                 {"ask": "read the supply obligations it is subject to", "tool": "get_provision"},
             ]},
        ],
    },
    "turns": [
        {"text": "Finding the ownership clause and the governing definition. ",
         "tools": [("find_provision", {"query": "New IPR ownership under a Contract"}),
                   ("define", {"term": "Central Buying Office"})]},
        {"text": "",
         "tools": [("get_provision", {"path": "core-terms/9/9.2"}),
                   ("follow_references", {"path": "core-terms/9/9.2", "direction": "outbound"}),
                   ("cite", {"path": "core-terms/9/9.2"})]},
        # Reading the two clauses the answer goes on to quote. Without this the
        # citation checker rejects them, which is the checker doing its job.
        {"text": "",
         "tools": [("get_provision", {"path": "core-terms/3/3.1/3.1.1/a"}),
                   ("get_provision", {"path": "core-terms/3/3.1/3.1.2"})]},
        {"text": (
            "Any new intellectual property created under a contract is owned by the "
            "[[term:Central Buying Office]] [[core-terms/9/9.2|2]] — the central purchasing "
            "authority the agreement defines at the front [[joint-schedule-1/2/table/2/1|1]].\n\n"
            "That ownership is not unconditional. It is expressly subject to the supply "
            "obligations in Clause 3.1: the [[term:Provider]] must supply [[term:Outputs]] that "
            "meet the requirement and comply with the law [[core-terms/3/3.1/3.1.1/a|1]], and "
            "must supply them with a warranty of at least 90 days from handover against all "
            "obvious defects [[core-terms/3/3.1/3.1.2|1]].\n\n"
            "Two things in that same sentence I could not settle. It also points at a "
            "\u201cSchedule 2\u201d without saying which one, and this document set contains more "
            "than one, so that is sitting in the review queue rather than in this answer. It "
            "cites the Bribery Act 2010, which is legislation rather than part of this agreement."
                ), "tools": []},
    ],
}

CLAUSE_92 = {
    "match": ("9.2", "clause 9.2"),
    "gate": "lookup",
    "plan": {
        "restated": "Read Clause 9.2 and quote it.",
        "batches": [{"n": 1, "why": "a single provision, one call",
                     "queries": [{"ask": "read Clause 9.2", "tool": "get_provision"}]}],
    },
    "turns": [
        {"text": "", "tools": [("get_provision", {"path": "core-terms/9/9.2"}),
                               ("cite", {"path": "core-terms/9/9.2"})]},
        {"text": (
            "Clause 9.2 reads: \u201cAny New IPR created under a Contract is owned by the Central "
            "Buying Office subject to Clauses 3.1.1 and 3.1.2, Schedule 2 and the Bribery Act "
            "2010.\u201d [[core-terms/9/9.2|2]]\n\n"
            "One thing is worth knowing about it: the numbering jumps here — 9.4 follows 9.2 in "
            "the document, with no 9.3 [[core-terms/9/9.2|2]]."
                ), "tools": []},
    ],
}

FALLBACK = {
    "match": (),
    "gate": "research",
    "plan": {
        "restated": "Find the provisions that match this question and read the best one.",
        "batches": [{"n": 1, "why": "search first", "queries": [
            {"ask": "search paths, titles and terms", "tool": "find_provision"}]}],
    },
    "turns": [
        {"text": "", "tools": [("find_provision", {"query": "provision"})]},
        {"text": (
            "This scripted demo only carries worked answers for the two example questions. "
            "The search ran for real against the fixtures backend, but no answer is scripted "
            "for this question, so there is nothing here I can cite."
        ), "tools": []},
    ],
}

SCRIPTS = (IPR, CLAUSE_92)


def _script_for(question: str) -> dict:
    q = question.lower()
    for s in SCRIPTS:
        if any(m in q for m in s["match"]):
            return s
    return FALLBACK


def _question_of(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


def _round_of(messages: list[dict]) -> int:
    """Which agent turn this is. The loop appends an assistant message and a
    tool-result message per round, so the count tells us where we are without
    holding any state across concurrent requests."""
    return (len(messages) - 1) // 2


# --------------------------------------------------------------------------
# the llm_client-shaped surface
# --------------------------------------------------------------------------
def complete(*, task: str, model: str, system: str, messages: list[dict], **kw):
    from .llm_client import LLMResponse

    question = _question_of(messages) or (messages[-1].get("content") if messages else "")
    if task == "chat_gate":
        return LLMResponse(text=_script_for(str(question))["gate"], stop_reason="end_turn")
    if task == "chat_plan":
        body = str(question)
        if "\nQuestion: " in body:
            body = body.split("\nQuestion: ", 1)[1]
        return LLMResponse(text=json.dumps(_script_for(body)["plan"]), stop_reason="end_turn")
    return LLMResponse(text="", stop_reason="end_turn")


def stream(*, task: str, model: str, system: str, messages: list[dict],
           tools: list[dict] | None = None, **kw) -> Iterator[tuple[str, Any]]:
    from .llm_client import LLMResponse

    script = _script_for(_question_of(messages))
    turns = script["turns"]
    turn = turns[min(_round_of(messages), len(turns) - 1)]

    text = turn["text"]
    for chunk in _chunks(text):
        yield ("text", chunk)

    tool_uses = [
        {"id": f"s{_round_of(messages)}_{i}", "name": name, "input": args}
        for i, (name, args) in enumerate(turn["tools"])
    ]
    yield ("done", LLMResponse(
        text=text,
        tool_uses=tool_uses,
        stop_reason="tool_use" if tool_uses else "end_turn",
        model=f"{model} (scripted)",
    ))


def _chunks(text: str, size: int = 24) -> Iterator[str]:
    """Emit in small pieces so the page's streaming path is exercised."""
    for i in range(0, len(text), size):
        yield text[i:i + size]
