"""Typed ambiguity routing: one narrow prompt per failure mode.

DESIGN tier 2: "Different kinds of ambiguity go to different prompts, since a
model asked 'is this capital a sentence start' and a model asked 'is this
capital a typo' are answering different questions." So there is no general
"is this a term use" prompt here. Each of the four `AmbiguityKind` values gets a
prompt written for its own failure mode, with its own decision rule and its own
worked example, and items are batched per kind.

The model's answer can do three things, and the asymmetry EVALUATION.md section 2
insists on is built into which: confirming a use makes it `confident` with
`method: "llm"`; saying it is not a use **only** removes it when the model is at
least `REJECT_MIN_CONFIDENCE` sure, because a false negative hides an obligation
from the person searching for it and is the costlier error here; anything else
stays `ambiguous` and goes to the review queue, which is the honest destination
for a case nobody settled.

Confidence is elicited in the same structured response as the verdict and, per
EVALUATION.md layer 5, before it: each object states `confidence` ahead of
`verdict`, so the score is not a defence of a conclusion already committed to.

Model choice is a gap in `config.py`, recorded rather than papered over.
`config.MODELS` has no vocabulary entry, and DESIGN's model table lists stage 4
as deterministic because the routed checks were costed with the reference
residue. This module therefore uses `reference_residue` (Claude Haiku 4.5,
"bounded choice among presented candidates"), which is exactly the shape of
these questions, and the run summary says so.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from pipeline.vocabulary.llmio import Runner, strip_fence
from pipeline.vocabulary.matching import Match

# config.MODELS has no key for stage 4's routed checks. See the module docstring.
TASK = "reference_residue"
TASK_NOTE = ("config.MODELS has no vocabulary/term-disambiguation entry; stage 4's "
             "routed checks use the reference_residue model, which is the same "
             "bounded-choice shape. Flagged for the orchestrator.")
PROMPT_VERSION = "vocab-routing-v1"
BATCH_SIZE = 20
SENTENCE_CLIP = 600
DEFINITION_CLIP = 400

# Removing a use the deterministic matcher found is the costlier error
# (EVALUATION.md section 2), so it needs a confident model, not a bare majority.
REJECT_MIN_CONFIDENCE = 0.8

_COMMON_TAIL = (
    "\n\nReply with a JSON array and nothing else. One object per item, in the "
    "same order, shaped exactly:\n"
    '{"i": <index>, "confidence": <0.0-1.0>, "verdict": "use" | "not_a_use" | '
    '"unsure", "governing_term": "<term>", "why": "<one short sentence>"}\n'
    "State `confidence` before `verdict`: score how sure you are of the evidence "
    "first, then commit. Use \"unsure\" whenever the text does not settle it; an "
    "honest \"unsure\" is worth more than a guess, and those go to a human.\n"
    "`governing_term` repeats the candidate term unless the item asks you to "
    "choose between several, in which case name the one that governs.")

PROMPTS: dict[str, str] = {
    "sentence_initial": (
        "You are checking capitalised words in a UK public-sector framework "
        "agreement. In this contract a capitalised phrase is usually a defined "
        "term, but a word at the start of a sentence is capitalised for that "
        "reason alone, so the capital is not evidence there.\n\n"
        "For each item, the phrase sits at the start of a sentence or of a "
        "lettered sub-paragraph. Decide whether the writer meant the DEFINED "
        "term (whose definition is quoted for you) or the ordinary English word "
        "that happens to start the sentence. Read the rest of the sentence: if "
        "the sentence only makes sense with the defined meaning, it is a use.\n\n"
        "Example. \"Default by the Supplier shall entitle the Buyer to ...\" uses "
        "the defined term Default. \"Default settings may be changed at any "
        "time\" does not."),
    "heading": (
        "You are checking capitalised words in a UK public-sector framework "
        "agreement. In this contract a capitalised phrase is usually a defined "
        "term, but headings are capitalised throughout by typographic "
        "convention, so a capital inside a heading is not evidence.\n\n"
        "For each item the phrase appears in a heading or title rather than in "
        "the body of a provision. Decide whether the heading is naming the "
        "defined term (whose definition is quoted for you) or using an ordinary "
        "word that the heading style happens to capitalise.\n\n"
        "Example. A heading \"Intellectual Property Rights\" names the defined "
        "term where the pack defines it. A heading \"What Has To Be Provided\" "
        "capitalises every word and names no defined term at all."),
    "typo_dense": (
        "You are checking capitalised words in a UK public-sector framework "
        "agreement, in a section whose spelling is measurably unreliable: this "
        "section trips a deterministic typo detector well above the corpus rate. "
        "Where spelling is unreliable, capitalisation stops being evidence in "
        "BOTH directions. A stray capital can invent a term use that was never "
        "meant, and a missing capital can hide a real one.\n\n"
        "For each item, ignore the capitalisation and read the sense. Does the "
        "provision rely on the defined meaning (quoted for you), or is the "
        "phrase ordinary English that a typo has capitalised? Say so plainly, "
        "and say \"unsure\" when the surrounding text is too damaged to tell.\n\n"
        "Example. \"the rFramework Contract\" is the defined term with a stray "
        "character in front of it. \"the buyer may request\" in a section that "
        "elsewhere writes \"Buyer\" is probably the defined term with its capital "
        "lost, but only the sense of the sentence can settle it."),
    "alias_collision": (
        "You are checking abbreviations in a UK public-sector framework "
        "agreement. Defined terms are introduced with a parenthetical "
        "abbreviation at first use, and after that the abbreviation IS the term. "
        "The abbreviation in each item below could belong to more than one "
        "defined term, and every candidate is listed with its definition.\n\n"
        "For each item, choose which defined term this occurrence binds to, or "
        "say \"unsure\" if the provision does not settle it. Name your choice in "
        "`governing_term`, spelled exactly as the candidate is spelled. Answer "
        "\"not_a_use\" only if the letters are not this contract's abbreviation "
        "at all here."),
}


def _clip(text: Optional[str], limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " […truncated]"


@dataclass
class RoutedItem:
    index: int
    match: Match
    payload: dict


@dataclass
class Verdict:
    index: int
    verdict: str
    confidence: Optional[float]
    governing_term: Optional[str]
    why: str
    malformed: bool = False


@dataclass
class RoutingResult:
    kind: str
    items: list[RoutedItem] = field(default_factory=list)
    batches: list[dict] = field(default_factory=list)
    verdicts: dict[int, Verdict] = field(default_factory=dict)
    state: str = "pending"
    note: str = ""


def item_payload(match: Match, definition_text: Callable[[str], Optional[str]],
                 candidates_for: Callable[[str], list[str]]) -> dict:
    """What the checker is shown. The sentence is clipped with a marker: a
    checker shown text cut off mid-clause with no marker may call a correct
    decision wrong because the evidence looks missing."""
    payload = {
        "term": match.term,
        "matched_text": match.surface,
        "path": match.node_path,
        "field": match.field_name,
        "char_span": list(match.span),
        "sentence": _clip(match.sentence, SENTENCE_CLIP),
        "definition": _clip(definition_text(match.term), DEFINITION_CLIP),
    }
    if match.collides_with:
        payload["candidate_terms"] = [
            {"term": t, "definition": _clip(definition_text(t), DEFINITION_CLIP)}
            for t in match.collides_with]
    else:
        payload["candidate_terms"] = [
            {"term": t, "definition": _clip(definition_text(t), DEFINITION_CLIP)}
            for t in candidates_for(match.term)]
    return payload


def build_prompt(kind: str, items: list[RoutedItem]) -> str:
    head = PROMPTS[kind]
    body = json.dumps([{"i": it.index, **it.payload} for it in items],
                      indent=1, ensure_ascii=False)
    return f"{head}{_COMMON_TAIL}\n\nItems:\n{body}"


def parse_verdicts(raw: str, valid: set[int]) -> tuple[list[Verdict], Optional[str]]:
    try:
        parsed = json.loads(strip_fence(raw))
        if not isinstance(parsed, list):
            raise ValueError("checker did not return a JSON array")
    except Exception as exc:                               # noqa: BLE001
        return [], f"unparseable checker response: {type(exc).__name__}: {exc}"
    out: list[Verdict] = []
    for row in parsed:
        if not isinstance(row, dict) or not isinstance(row.get("i"), int) \
                or row["i"] not in valid:
            out.append(Verdict(index=-1, verdict="unsure", confidence=None,
                               governing_term=None, why="", malformed=True))
            continue
        verdict = row.get("verdict")
        conf = row.get("confidence")
        out.append(Verdict(
            index=row["i"],
            verdict=verdict if verdict in ("use", "not_a_use", "unsure") else "unsure",
            confidence=float(conf) if isinstance(conf, (int, float)) else None,
            governing_term=row.get("governing_term") if isinstance(
                row.get("governing_term"), str) else None,
            why=str(row.get("why", ""))[:300],
            malformed=verdict not in ("use", "not_a_use", "unsure")))
    return out, None


def route(matches: list[Match], runner: Runner,
          definition_text: Callable[[str], Optional[str]],
          candidates_for: Callable[[str], list[str]]) -> dict[str, RoutingResult]:
    """Route every ambiguous match, by kind. Deterministic in item order."""
    queues: dict[str, RoutingResult] = {}
    index = 0
    for match in matches:
        if match.status != "ambiguous" or match.ambiguity_kind == "none":
            continue
        queue = queues.setdefault(match.ambiguity_kind, RoutingResult(kind=match.ambiguity_kind))
        queue.items.append(RoutedItem(index=index, match=match,
                                      payload=item_payload(match, definition_text,
                                                           candidates_for)))
        index += 1

    for kind in sorted(queues):
        queue = queues[kind]
        for start in range(0, len(queue.items), BATCH_SIZE):
            batch = queue.items[start:start + BATCH_SIZE]
            prompt = build_prompt(kind, batch)
            call = runner.complete(TASK, f"{PROMPT_VERSION}:{kind}", prompt)
            record = {"items": [it.index for it in batch],
                      "call": call.as_dict(), "prompt": prompt}
            if call.ok:
                verdicts, error = parse_verdicts(call.response, {it.index for it in batch})
                if error:
                    record["parse_error"] = error
                for v in verdicts:
                    if not v.malformed or v.index >= 0:
                        queue.verdicts[v.index] = v
                record["verdicts"] = [v.__dict__ for v in verdicts]
            queue.batches.append(record)
        states = {b["call"]["state"] for b in queue.batches}
        parse_errors = [b for b in queue.batches if b.get("parse_error")]
        if states <= {"replayed", "called"}:
            # A batch whose reply would not parse was called but not checked;
            # calling that "checked" would report an agreement nobody measured.
            queue.state = "checked_with_parse_errors" if parse_errors else "checked"
        else:
            queue.state = sorted(states)[0]
        queue.note = "; ".join(sorted(
            {b["call"]["note"] for b in queue.batches}
            | {b["parse_error"] for b in parse_errors}))
    return queues


def apply(matches: list[Match], queues: dict[str, RoutingResult]
          ) -> tuple[list[Match], list[dict]]:
    """Fold verdicts back into the matches. Returns (kept, rejected records)."""
    by_index: dict[int, Verdict] = {}
    match_of: dict[int, Match] = {}
    for queue in queues.values():
        for item in queue.items:
            match_of[item.index] = item.match
            if item.index in queue.verdicts:
                by_index[item.index] = queue.verdicts[item.index]

    decided: dict[int, Verdict] = by_index
    rejected: list[dict] = []
    reject_ids: set[int] = set()
    for index, verdict in decided.items():
        match = match_of[index]
        if verdict.verdict == "use":
            match.status = "confident"
            match.ambiguity_kind = "none"
            match.method = "llm"
            if verdict.governing_term and verdict.governing_term in match.collides_with:
                match.term = verdict.governing_term
        elif verdict.verdict == "not_a_use" and \
                (verdict.confidence or 0.0) >= REJECT_MIN_CONFIDENCE:
            reject_ids.add(id(match))
            rejected.append({**match.as_dict(), "why": verdict.why,
                             "confidence": verdict.confidence,
                             "rule": f"removed: checker said not_a_use at confidence "
                                     f">= {REJECT_MIN_CONFIDENCE}"})
    kept = [m for m in matches if id(m) not in reject_ids]
    return kept, rejected


def summarise(queues: dict[str, RoutingResult]) -> dict:
    return {
        "task": TASK, "task_note": TASK_NOTE, "prompt_version": PROMPT_VERSION,
        "batch_size": BATCH_SIZE,
        "reject_min_confidence": REJECT_MIN_CONFIDENCE,
        "queues": {kind: {"items": len(q.items), "batches": len(q.batches),
                          "verdicts": len(q.verdicts), "state": q.state,
                          "note": q.note}
                   for kind, q in sorted(queues.items())},
    }
