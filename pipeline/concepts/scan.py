"""The concept scan: one model call per scan unit, over its full subtree text.

SPEC 2.4: "The scan unit is a part or top level clause with its full derived
subtree text, long enough context to see how language is actually used."
DESIGN tier 3 says the same in prose, "An LLM reads each part and top level
clause, with enough surrounding text to see how terms are actually used rather
than isolated sentences". So the units are every part plus every top-level child
of every part, which is exactly the set `pipeline/eval/sections/concepts.py`
counts coverage over, and the two must agree or coverage would be measured
against a denominator the scan never used.

Confidence is elicited in the same response as the concepts, per SPEC 2.4, and
inside each concept object before its provisions, per EVALUATION.md layer 5:
"scored before it commits to a final answer so it is not defending a conclusion
it already stated".

Two rules are enforced on the way back in, not asked for politely in the prompt:

* a concept may only claim provisions that exist, and a path the model invents
  is dropped and logged, because tier 3 never mints tier 1 nodes;
* a concept with no surviving member is not minted at all.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from pipeline.schemas import Node
from pipeline.vocabulary import treeio
from pipeline.vocabulary.llmio import Runner, strip_fence

TASK = "concepts"
PROMPT_VERSION = "concept-scan-v1"

# Enough context to see how the language is used, bounded so one very long part
# cannot blow the window. Clipping is marked, never silent.
SOURCE_CLIP = 40_000
MAX_CONCEPTS = 8

PROMPT = (
    "You are reading one unit of a UK public-sector framework agreement and "
    "naming the concepts it is about, so that an agent can narrow a large graph "
    "to the right neighbourhood before running precise queries inside it.\n\n"
    "What a concept is here: a theme the provisions are about, in the reading of "
    "someone who works with these contracts. Termination triggers. Supplier "
    "insolvency risk. Exit management. Concepts are for navigation and are never "
    "quoted as authority, so name neighbourhoods, not sentences.\n\n"
    "Rules:\n"
    f"- At most {MAX_CONCEPTS} concepts. Fewer is better than padded.\n"
    "- A concept label is a short lower-case noun phrase, two to five words.\n"
    "- Do NOT propose a concept whose label is simply a capitalised defined term "
    "of this contract. Defined terms are handled elsewhere and outrank concepts.\n"
    "- List the provisions the concept covers by their exact `path` from the "
    "PROVISIONS list below. Never invent a path.\n"
    "- `relations` join two concepts you are proposing in this same response. "
    "`relation` is a VERB PHRASE describing the link, such as depends_on, "
    "constrains or triggers, and is never a concept label. `to` is the exact "
    "`label` of another concept in this same response. Omit `relations` "
    "entirely rather than inventing a link.\n"
    "- State `confidence` (0.0 to 1.0) for each concept BEFORE listing its "
    "provisions: score the evidence first, then commit. A concept you are "
    "guessing at should carry a low score, not be omitted.\n\n"
    "Reply with a JSON object and nothing else:\n"
    + '{"concepts": [{"label": "...", "confidence": 0.0, '
      '"provisions": ["path", ...], '
      '"relations": [{"relation": "depends_on", "to": "<label of another '
      'concept in this response>"}]}]}'
    + "\n\nUNIT {path} ({kind}){title}\n\n"
      "PROVISIONS (path :: text):\n{provisions}\n\n"
      "FULL TEXT OF THE UNIT:\n{source}")

# `.format` is applied to the tail only: the JSON example above is literal text
# and its braces are not placeholders.
_HEAD, _TAIL = PROMPT.split("\n\nUNIT {path}", 1)
_TAIL = "\n\nUNIT {path}" + _TAIL


@dataclass
class ScanUnit:
    part: str
    node: Node

    @property
    def path(self) -> str:
        return self.node.path


@dataclass
class ProposedConcept:
    label: str
    confidence: float
    member_node_ids: list[str]
    member_paths: list[str]
    relations: list[dict]
    scope_path: str
    part: str
    unit_kind: str

    @property
    def id(self) -> str:
        return concept_id(self.scope_path, self.label)


@dataclass
class ScanResult:
    unit: ScanUnit
    proposed: list[ProposedConcept] = field(default_factory=list)
    state: str = ""
    note: str = ""
    dropped_paths: list[str] = field(default_factory=list)
    parse_error: Optional[str] = None
    prompt: str = ""


def normalise_label(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip().casefold()


def concept_id(scope_path: str, label: str) -> str:
    """Deterministic in the scope and the label, so a rerun that proposes the
    same concept updates it rather than minting a twin (DESIGN section 4)."""
    digest = hashlib.sha1(f"{scope_path}|{normalise_label(label)}".encode()).hexdigest()
    return f"concept-{digest[:16]}"


def units(trees: treeio.Trees) -> list[ScanUnit]:
    """Every part, and every top-level child of every part."""
    out: list[ScanUnit] = []
    for pid, part in trees.ordered():
        out.append(ScanUnit(part=pid, node=part))
        out.extend(ScanUnit(part=pid, node=child)
                   for child in treeio.anatomy_children(part))
    return out


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " […truncated]"


def provisions_of(unit: ScanUnit) -> list[Node]:
    """The citable text-bearing nodes a concept may claim as members."""
    return [n for n in treeio.walk(unit.node)
            if n.kind != "ref" and n.citable and (n.text or n.title)]


def build_prompt(unit: ScanUnit) -> str:
    listing = "\n".join(
        f"{n.path} :: {_clip((n.text or n.title or '').strip(), 300)}"
        for n in provisions_of(unit))
    title = f' titled "{unit.node.title}"' if unit.node.title else ""
    return _HEAD + _TAIL.format(
        path=unit.path, kind=unit.node.kind, title=title, provisions=listing,
        source=_clip(treeio.subtree_text(unit.node), SOURCE_CLIP))


def parse(raw: str, unit: ScanUnit, by_path: dict[str, Node]) -> ScanResult:
    result = ScanResult(unit=unit)
    try:
        payload = json.loads(strip_fence(raw))
        proposals = payload["concepts"] if isinstance(payload, dict) else payload
        if not isinstance(proposals, list):
            raise ValueError("`concepts` is not a list")
    except Exception as exc:                               # noqa: BLE001
        result.parse_error = f"{type(exc).__name__}: {exc}"
        return result
    for row in proposals:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            result.parse_error = "a proposed concept was not an object with a label"
            continue
        label = row["label"].strip()
        if not label:
            continue
        paths, ids = [], []
        for path in row.get("provisions", []) or []:
            node = by_path.get(path) if isinstance(path, str) else None
            if node is None:
                result.dropped_paths.append(str(path))
                continue
            paths.append(path)
            ids.append(node.id)
        if not ids:
            result.dropped_paths.append(f"{label}: no valid provision")
            continue
        confidence = row.get("confidence")
        confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
        # `relation` is the documented key; `label` is accepted because models
        # reach for it, and resolve.py then checks the value really is a verb
        # phrase and not a concept label repeated in the wrong field.
        relations = []
        for r in (row.get("relations") or []):
            if not isinstance(r, dict) or not isinstance(r.get("to"), str):
                continue
            verb = r.get("relation") if isinstance(r.get("relation"), str) \
                else r.get("label")
            if isinstance(verb, str) and verb.strip():
                relations.append({"relation": verb.strip(), "to": r["to"].strip()})
        result.proposed.append(ProposedConcept(
            label=label, confidence=max(0.0, min(1.0, confidence)),
            member_node_ids=list(dict.fromkeys(ids)),
            member_paths=list(dict.fromkeys(paths)), relations=relations,
            scope_path=unit.path, part=unit.part, unit_kind=unit.node.kind))
    return result


def scan(trees: treeio.Trees, runner: Runner) -> list[ScanResult]:
    by_path = trees.by_path()
    results: list[ScanResult] = []
    for unit in units(trees):
        if not treeio.subtree_text(unit.node).strip():
            results.append(ScanResult(unit=unit, state="skipped_no_text",
                                      note="the unit carries no text to scan"))
            continue
        prompt = build_prompt(unit)
        call = runner.complete(TASK, PROMPT_VERSION, prompt)
        if not call.ok:
            results.append(ScanResult(unit=unit, state=call.state, note=call.note,
                                      prompt=prompt))
            continue
        result = parse(call.response, unit, by_path)
        result.state, result.note, result.prompt = call.state, call.note, prompt
        results.append(result)
    return results
