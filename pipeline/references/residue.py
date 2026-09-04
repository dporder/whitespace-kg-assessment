"""The residue: what the rules could not settle, and what happens to it.

SPEC 2.2: "LLM residue calls present the top 5 candidates together and must
accept `NONE`, and a `NONE` keeps status unresolved with candidates kept."
SPEC 2.4 adds that the model "must emit its score in the same structured
response as its ranked candidates, scored before it commits to a final answer
so it is not defending a conclusion it already stated".

So the response schema is ordered: what it considered, then its confidence,
then its answer. The prompt says so explicitly and the parser reads the keys by
name, so a model that reorders them is still read correctly but the elicitation
order is the one the spec asks for.

Degradation is the point of this module tonight. The API key in
`config.ENV_FILE` is identity-linked and every call is refused until
`ANTHROPIC_WORKSPACE_ID` is supplied. When that happens nothing here guesses:
the deterministic outcome stands untouched, the ref keeps its status and its
candidates, and it picks up a `llm_queued:` marker in `anomalies` plus a row in
`refs/llm_queue.json`. One rerun once the id lands finishes the queue, and
everything already answered replays from `pipeline.llm`'s cache for free.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from pipeline import llm
from pipeline.schemas import Candidate, Node

from .corpus import Corpus

TASK = "reference_residue"
HARD_TASK = "reference_hard"
QUEUE_MARKER = "llm_queued"
TOP_N = 5

SYSTEM = ("You resolve cross-references in UK public-sector framework agreements. "
          "You choose only from the candidates you are shown, and NONE is a normal, "
          "expected answer whenever the evidence does not single one out.")


def _context_block(ref: Node, ctx: dict) -> str:
    lines = [
        f"CITING PROVISION: {ctx.get('parent_path')}",
        f"  part: {ctx.get('part')}"
        + (f"   unit label: {ctx['unit_label']}" if ctx.get("unit_label") else ""),
        f"  sentence: {ctx.get('sentence') or '(not captured)'}",
        f"POINTING WORDS: {ref.text!r}",
        f"  detected kind: {ref.ref_kind}   scope rule applied: {ref.scope_rule}",
        f"  deterministic outcome: {ref.status}",
    ]
    for note in ctx.get("notes") or []:
        lines.append(f"  why it is unsettled: {note}")
    return "\n".join(lines)


def build_prompt(ref: Node, ctx: dict, candidates: list[Candidate]) -> str:
    rows = "\n".join(
        f"  {i}. {c.path}   (deterministic score {c.score}) — {c.reason or 'no reason recorded'}"
        for i, c in enumerate(candidates, start=1))
    return f"""{_context_block(ref, ctx)}

CANDIDATES, all of them, ranked by the deterministic pass:
{rows}

Choose exactly one candidate path, or NONE.

Rules:
- NONE is valid and expected. Answer NONE if the text does not single out one
  candidate. A wrong resolution is worse than an honest abstention.
- You may only answer with one of the candidate paths listed above, exactly as
  written, or the string NONE. Never invent a path.
- Score your confidence before you commit to an answer.

Reply with one JSON object with exactly these keys, in this order:
  "considered": [{{"path": "<candidate path>", "for": "<evidence for>",
                  "against": "<evidence against>"}}],
  "confidence": <number between 0 and 1, how likely your answer is correct>,
  "answer": "<one candidate path>" or "NONE"
"""


def _parse(raw: Any, candidates: list[Candidate]) -> tuple[Optional[str], Optional[float], str]:
    """(answer, confidence, note). An off-list answer is a failure, not a target."""
    if not isinstance(raw, dict):
        return None, None, "checker did not return a JSON object"
    answer = raw.get("answer")
    confidence = raw.get("confidence")
    try:
        confidence = None if confidence is None else round(float(confidence), 3)
    except (TypeError, ValueError):
        confidence = None
    if not isinstance(answer, str):
        return None, confidence, "response carried no string answer"
    answer = answer.strip()
    if answer.upper() == "NONE":
        return "NONE", confidence, "model answered NONE"
    if answer not in {c.path for c in candidates}:
        return None, confidence, f"model answered {answer!r}, which is not a candidate"
    return answer, confidence, "model chose a candidate"


def _apply(ref: Node, corpus: Corpus, answer: str, confidence: Optional[float],
           note: str) -> None:
    ref.resolver = "llm"
    ref.confidence = confidence
    if answer == "NONE":
        # NONE keeps it unresolved with the candidates attached (SPEC 2.2).
        ref.status = "unresolved"
        ref.target_path = None
        _note(ref, "llm_answered_none: candidates kept, target left unset")
        return
    if corpus.exists(answer):
        ref.status = "resolved"
        ref.target_path = answer
        _note(ref, f"llm_chose: {answer}")
        return
    # A right answer this run cannot record: refs never mint target nodes, so a
    # choice outside the corpus stays unresolved with that candidate on top.
    ref.status = "unresolved"
    ref.target_path = None
    ref.candidates = [Candidate(path=c.path, score=min(1.0, c.score + 0.05) if c.path == answer
                                else c.score, reason=c.reason) for c in ref.candidates]
    _note(ref, f"llm_chose_uningested_target: {answer}; kept unresolved because a ref "
               f"never mints its target")


def _note(ref: Node, text: str) -> None:
    if text not in ref.anomalies:
        ref.anomalies.append(text)


def selectable(ref: Node) -> bool:
    """Residue is what the rules left unsettled *and* could offer a choice on.

    An unresolved ref with no candidates gives a model nothing to choose
    between, so it goes straight to the review queue rather than to a prompt
    that invites invention.
    """
    return ref.status in ("ambiguous", "unresolved") and bool(ref.candidates)


def run(refs: list[Node], contexts: dict[str, dict], corpus: Corpus, *,
        no_llm: bool = False) -> dict:
    """Ask about every residue ref. Returns the report; mutates refs in place."""
    queue: list[dict] = []
    report = {"task": TASK, "hard_task": HARD_TASK, "considered": 0, "called": 0,
              "resolved": 0, "answered_none": 0, "chose_uningested_target": 0,
              "escalated": 0, "queued": 0, "failed": 0,
              "prompt_version": llm.PROMPT_VERSIONS[TASK],
              "skipped_no_candidates": 0, "reason": None, "queue": queue}

    candidates_by_ref = {}
    for ref in refs:
        if ref.status in ("ambiguous", "unresolved") and not ref.candidates:
            report["skipped_no_candidates"] += 1
        if not selectable(ref):
            continue
        report["considered"] += 1
        candidates_by_ref[ref.path] = sorted(
            ref.candidates, key=lambda c: (-c.score, c.path))[:TOP_N]

    if not candidates_by_ref:
        return report
    spend_before = llm.stats()

    if no_llm:
        report["reason"] = "--no-llm: the residue was not sent to a model"
    elif not llm.available():
        report["reason"] = llm.unavailable_reason() or "llm unavailable"

    for ref in refs:
        candidates = candidates_by_ref.get(ref.path)
        if candidates is None:
            continue
        ctx = contexts.get(ref.path, {})
        if report["reason"]:
            _queue(ref, queue, report, report["reason"])
            continue
        task = HARD_TASK if _is_hard(ref) else TASK
        outcome = _ask(task, ref, ctx, candidates, report)
        if outcome is None:
            _queue(ref, queue, report, report["reason"] or "llm call failed")
            continue
        answer, confidence, note = outcome
        if answer is None:
            report["escalated"] += 1
            escalated = _ask(HARD_TASK, ref, ctx, candidates, report)
            if escalated is None:
                _queue(ref, queue, report, report["reason"] or "llm call failed")
                continue
            answer, confidence, note = escalated
        if answer is None:
            report["failed"] += 1
            _note(ref, f"llm_unusable_response: {note}")
            continue
        _apply(ref, corpus, answer, confidence, note)
        # Counted by what happened, not by what was answered: a model naming a
        # target this run has not ingested has not resolved anything, and a
        # report that called that "resolved" would be the exact kind of
        # confidently wrong number this build exists to avoid.
        if ref.status == "resolved":
            report["resolved"] += 1
        elif answer == "NONE":
            report["answered_none"] += 1
        else:
            report["chose_uningested_target"] += 1
    report["spend"] = llm.stats_since(spend_before)
    return report


def _is_hard(ref: Node) -> bool:
    """The named hard case gets the bigger model on the first call (DESIGN 10)."""
    return any(a.startswith("mislabelled_cross_reference") for a in ref.anomalies)


def _ask(task: str, ref: Node, ctx: dict, candidates: list[Candidate],
         report: dict) -> Optional[tuple[Optional[str], Optional[float], str]]:
    prompt = build_prompt(ref, ctx, candidates)
    try:
        raw = llm.structured(task, prompt, system=SYSTEM,
                             prompt_version=llm.PROMPT_VERSIONS.get(task))
    except llm.LLMUnavailable as exc:
        report["reason"] = report["reason"] or str(exc)
        return None
    except llm.LLMResponseError as exc:
        return None, None, f"unparseable response: {exc}"
    report["called"] += 1
    return _parse(raw, candidates)


def _queue(ref: Node, queue: list[dict], report: dict, reason: str) -> None:
    """Keep the deterministic outcome, mark the ref, and remember the question."""
    _note(ref, f"{QUEUE_MARKER}:{TASK}: {reason}")
    queue.append({"ref_path": ref.path, "status": ref.status, "ref_kind": ref.ref_kind,
                  "text": ref.text,
                  "candidates": [c.model_dump() for c in ref.candidates],
                  "reason": reason})
    report["queued"] += 1


def queue_file(report: dict) -> dict:
    return {"task": report["task"], "prompt_version": report["prompt_version"],
            "reason": report.get("reason"), "count": len(report["queue"]),
            "note": ("rerun `python -m pipeline.references --reresolve` once the model "
                     "is reachable; answers already given replay from the llm cache"),
            "items": report["queue"]}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)
