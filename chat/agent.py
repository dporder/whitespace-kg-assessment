"""Gate, plan, bounded tool loop. The three pieces of SPEC 6, kept small.

The shape is deliberate. A cheap classifier decides whether the question is a
plain lookup or needs research, and fails open to research. A planning step
restates the question as focused sub-queries with an explicit parallel batch
structure, which is also what the user sees as "working on". Then a bounded
loop over the seven tools, which are the only data access there is.

Citations are machine-checked, not merely requested: the model must write them
as [[path|page]], and every one is looked up in the CitationLedger of what the
tools actually returned before the answer is handed over.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

import config as pipeline_config

from . import config as ui_config
from . import llm_client, scripted
from .backends import get_backend
from .tools import TOOL_SCHEMAS, ToolRunner, crop_url

CITE_RE = re.compile(r"\[\[([^\]|]+)\|([^\]]*)\]\]")

GATE_SYSTEM = """You route questions about one UK public-sector framework agreement.

Answer with exactly one word.

lookup   the question names one provision and wants its content, nothing more.
         "What does Clause 9.2 say", "show me 3.1.2".
research anything else: obligations, consequences, what depends on what,
         definitions in context, comparisons, or any question naming more than
         one thing or needing a chain of reasoning.

If you are not sure, answer research."""

PLAN_SYSTEM = """You plan retrieval over a knowledge graph of one UK framework agreement.

Restate the user's question as focused sub-queries, grouped into batches. Every
sub-query in a batch must be runnable at the same time; a sub-query goes in a
later batch only when it genuinely needs an earlier batch's result (for example,
you cannot follow a provision's references until you know its path).

The available tools are:
  find_provision(query)                  fuzzy over paths, numbers, titles, terms
  get_provision(path)                    derived text, children, page, boxes
  follow_references(path, direction)     outbound or inbound refs
  define(term)                           definition text, source, governing site
  find_by_concept(label)                 model-derived concept neighbourhood
  history(lineage_key)                   version chain
  cite(path)                             page-image crop

Reply with JSON only, no prose and no code fence:

{"restated": "<one sentence>",
 "batches": [{"n": 1, "why": "<why these run together>",
              "queries": [{"ask": "<sub-query in words>", "tool": "<tool name>"}]}]}

At most 3 batches and at most 4 queries per batch. Prefer fewer."""

AGENT_SYSTEM = """You answer questions about one UK public-sector framework agreement (RM6116).

RULES, in order of importance.

1. The tools are your only source. You have no knowledge of this document
   beyond what a tool returns in this conversation. If the tools do not support
   a claim, do not make it.

2. Every claim carries a citation, written exactly as [[path|page]], where the
   path and the page are copied verbatim from a tool result. For example
   [[core-terms/9/9.2|2]]. Never compose a path or a page you have not seen in
   a tool result. A sentence you cannot cite is a sentence you do not write.
   These are machine-read and shown to the reader as a small numbered footnote,
   so put one immediately after the words it supports, not at the end of a
   paragraph covering several points.

2b. The first time you name a defined term you have looked up with `define`,
   wrap it as [[term:Central Buying Office]]. Only the first mention, only
   terms `define` actually returned. These render as a clickable term the
   reader can open; the reader never sees the brackets.

3. Quote only text returned by get_provision. find_provision returns paths, not
   text; get the provision before quoting it.

4. Report what is not settled. If a reference is ambiguous or unresolved, say
   so and name the candidates; do not pick one. If a term has a part-local
   definition that shadows the document-level one, say which governs where.

5. Concepts are model-derived navigation and are never citable. Cite the member
   provisions instead.

6. Never repair the document's text. Typos and stray characters are part of the
   contract; quote them as they are.

HOW TO WRITE IT. You are answering someone who needs a decision out of a long
agreement and has no interest in how you work. Write as a knowledgeable
colleague would: short sentences, everyday words, the answer first and the
qualifications after. Most answers want two to five sentences.

Never use the vocabulary of the machinery — no "node", "path", "graph",
"corpus", "resolver", "tool", "query", "index", "confidence score". Name
provisions the way the agreement names them ("Clause 9.2", "Joint Schedule 1"),
not by their path; the path belongs in the citation brackets only.

Say what you found and where, rather than presenting the document's contents as
your own assertion. If two provisions conflict, say so. If the evidence does
not settle the question, say that in one sentence rather than padding."""


@dataclass
class Citation:
    path: str
    page: int | None
    status: str                       # ok | page_mismatch | page_unparseable | unknown_path
    crop_url: str | None = None
    name: str | None = None           # how the agreement itself names it


@dataclass
class Turn:
    question: str
    route: str = "research"
    gate_reason: str = ""
    gate_failed_open: bool = False
    plan: dict = field(default_factory=dict)
    answer: str = ""
    citations: list[Citation] = field(default_factory=list)
    runner: ToolRunner | None = None


# --------------------------------------------------------------------------
# 1. the gate, cheap and fail-open
# --------------------------------------------------------------------------
def gate(question: str) -> tuple[str, str, bool]:
    """(route, reason, failed_open). Anything ambiguous defaults to research."""
    try:
        r = llm_client.complete(
            task="chat_gate",
            model=pipeline_config.MODELS["chat_gate"],
            system=GATE_SYSTEM,
            messages=[{"role": "user", "content": question}],
            max_tokens=8,
            temperature=0,
        )
    except llm_client.LLMUnavailable as exc:
        return "research", f"gate unavailable, failed open to research ({exc})", True
    word = r.text.strip().lower()
    if word.startswith("lookup"):
        return "lookup", "names one provision and wants its content", False
    if word.startswith("research"):
        return "research", "needs more than one provision or a chain of reasoning", False
    return "research", f"gate returned {word!r}, failed open to research", True


# --------------------------------------------------------------------------
# 2. the planning step, shown as "working on"
# --------------------------------------------------------------------------
def plan(question: str, route: str) -> dict:
    try:
        r = llm_client.complete(
            task="chat_plan",
            model=pipeline_config.MODELS["chat_plan"],
            system=PLAN_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"Route: {route}\nQuestion: {question}",
                }
            ],
            max_tokens=900,
            temperature=0,
        )
    except llm_client.LLMUnavailable as exc:
        return {
            "restated": question,
            "batches": [],
            "degraded": f"planner unavailable ({exc})",
        }
    text = r.text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"restated": question, "batches": [], "degraded": "planner returned non-JSON"}
    parsed.setdefault("restated", question)
    parsed.setdefault("batches", [])
    return parsed


# --------------------------------------------------------------------------
# 3. the bounded tool loop
# --------------------------------------------------------------------------
def _plan_brief(p: dict) -> str:
    if not p.get("batches"):
        return ""
    lines = [f"Your retrieval plan, restated: {p.get('restated', '')}"]
    for b in p["batches"]:
        asks = "; ".join(q.get("ask", "") for q in b.get("queries", []))
        lines.append(f"Batch {b.get('n')}: {asks}")
    lines.append("Run each batch's calls together where the API lets you.")
    return "\n".join(lines)


def run_turn(question: str, runner: ToolRunner | None = None) -> Iterator[tuple[str, Any]]:
    """Drive one question, yielding (event, payload) for the transport to stream."""
    turn = Turn(question=question)
    turn.runner = runner or ToolRunner()

    route, reason, failed_open = gate(question)
    turn.route, turn.gate_reason, turn.gate_failed_open = route, reason, failed_open
    yield ("gate", {"route": route, "reason": reason, "failed_open": failed_open})

    turn.plan = plan(question, route)
    yield ("plan", turn.plan)

    brief = _plan_brief(turn.plan)
    user_content = question if not brief else f"{question}\n\n---\n{brief}"
    messages: list[dict] = [{"role": "user", "content": user_content}]

    rounds = 0
    answer_parts: list[str] = []
    try:
        while rounds < ui_config.MAX_TOOL_ROUNDS:
            rounds += 1
            response = None
            for event, payload in llm_client.stream(
                task="chat_agent",
                model=pipeline_config.MODELS["chat_agent"],
                system=AGENT_SYSTEM,
                messages=messages,
                tools=TOOL_SCHEMAS,
                max_tokens=2048,
            ):
                if event == "text":
                    answer_parts.append(payload)
                    yield ("text", {"delta": payload})
                elif event == "done":
                    response = payload

            if response is None:
                yield ("error", {"message": "no response from the model"})
                return

            if not response.tool_uses:
                break

            assistant_content: list[dict] = []
            if response.text:
                assistant_content.append({"type": "text", "text": response.text})
            for tu in response.tool_uses:
                assistant_content.append(
                    {"type": "tool_use", "id": tu["id"], "name": tu["name"], "input": tu["input"]}
                )
            messages.append({"role": "assistant", "content": assistant_content})

            results: list[dict] = []
            for tu in response.tool_uses:
                if turn.runner.exhausted:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": json.dumps(
                                {"error": f"tool budget of {ui_config.MAX_TOOL_CALLS} calls spent"}
                            ),
                            "is_error": True,
                        }
                    )
                    continue
                call = turn.runner.run(tu["name"], tu["input"])
                yield (
                    "tool",
                    {
                        "name": call.name,
                        "args": call.args,
                        "ok": call.ok,
                        "summary": call.summary,
                        "ms": call.ms,
                    },
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": turn.runner.result_json(call),
                        "is_error": not call.ok,
                    }
                )
            messages.append({"role": "user", "content": results})
        else:
            yield ("note", {"message": f"stopped at the {ui_config.MAX_TOOL_ROUNDS}-round bound"})
    except llm_client.LLMUnavailable as exc:
        yield ("error", {"message": f"model unavailable: {exc}"})
        return

    turn.answer = "".join(answer_parts)
    turn.citations = verify_citations(turn.answer, turn.runner)
    yield (
        "citations",
        {
            "citations": [
                {"path": c.path, "page": c.page, "status": c.status,
                 "crop_url": c.crop_url, "name": c.name}
                for c in turn.citations
            ]
        },
    )
    yield (
        "done",
        {
            "rounds": rounds,
            "tool_calls": len(turn.runner.calls),
            "backend": turn.runner.backend.name,
            "llm": llm_client.backend_name(),
            "uncited_claims_possible": not turn.citations,
        },
    )


def verify_citations(answer: str, runner: ToolRunner) -> list[Citation]:
    """Every [[path|page]] in the answer, checked against what tools returned."""
    out: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    for path, page_s in CITE_RE.findall(answer):
        path = path.strip()
        page_s = page_s.strip()
        if (path, page_s) in seen:
            continue
        seen.add((path, page_s))
        try:
            page: int | None = int(page_s)
        except ValueError:
            page = None                       # fails verification, never passes
        status = runner.ledger.check(path, page)
        out.append(
            Citation(
                path=path,
                page=page,
                status=status,
                # a crop is offered only for a citation that actually checked out
                crop_url=crop_url(path) if status == "ok" else None,
                # taken from tool output like everything else: if no tool
                # reported a name for this path, the answer does not get one
                name=runner.ledger.names.get(path),
            )
        )
    return out


def describe_backend() -> dict:
    b = get_backend()
    return {
        "graph_backend": b.name,
        "data_source": ui_config.DATA_SOURCE,
        "llm": llm_client.backend_name(),
        "llm_available": llm_client.available(),
        "scripted": scripted.enabled(),
        "embedding_search": ui_config.EMBEDDING_SEARCH,
    }
