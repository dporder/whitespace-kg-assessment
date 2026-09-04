"""Step one of stage 3: find the pointing words. Nothing here resolves.

The fallback ladder, in the spec's order: grammar, then the orphan scan, then
LLM span extraction over orphan sentences only, then the review queue. This
module owns the first two rungs and hands the third what it is allowed to see.

Detection is scored separately from resolution (SPEC 2.2, EVALUATION layer 3),
so it writes its own file: `output/<run>/refs/detection/<part>.json`, holding
every pointer found, every orphan keyword triaged, and the sentences the LLM
rung would be given. Resolution's output is the `RefsFile` beside it.

One limit worth stating rather than hiding. A ref's `char_span` is an offset
into its parent's `text`, so a citation printed inside a heading's `title` has
nowhere to anchor: `schemas.Node` rejects ref children on a node with no text.
Those are collected as `title_citations` in the detection file and reported,
never silently dropped and never minted as refs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

from pipeline.schemas import Node

from . import grammar, legislation
from .corpus import walk


@dataclass
class Pointer:
    """One cited target, anchored to the characters that cite it."""
    parent_path: str
    part: str
    span: tuple[int, int]
    text: str
    ref_kind: str
    unit: str
    method: str = "grammar"
    family: Optional[str] = None
    number: Optional[str] = None
    item: Optional[str] = None
    title_paren: Optional[str] = None
    context: Optional[dict] = None
    group_id: Optional[str] = None
    anaphoric: bool = False
    expansion_index: Optional[int] = None
    legislation: Optional[dict] = None
    provision: Optional[str] = None
    sentence: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    order: int = 0

    @property
    def path(self) -> str:
        """`<parent>/ref@<start>-<end>`, plus `+<n>` for a range's interior.

        The members a range mints between its endpoints own no characters of
        their own, so two of them would otherwise share one path and one id,
        and the graph's uniqueness constraint would silently collapse them into
        one ref. The suffix appears only on those members. Flagged to the
        orchestrator as a gap in the path format pinned by SPEC 2.2.
        """
        base = f"{self.parent_path}/ref@{self.span[0]}-{self.span[1]}"
        return base if self.expansion_index is None else f"{base}+{self.expansion_index}"


@dataclass
class PartDetection:
    part: str
    pointers: list[Pointer] = field(default_factory=list)
    orphans: list[dict] = field(default_factory=list)
    title_citations: list[dict] = field(default_factory=list)
    llm_sentences: list[dict] = field(default_factory=list)

    def counts(self) -> dict:
        by_kind: dict[str, int] = {}
        by_method: dict[str, int] = {}
        for p in self.pointers:
            by_kind[p.ref_kind] = by_kind.get(p.ref_kind, 0) + 1
            by_method[p.method] = by_method.get(p.method, 0) + 1
        by_verdict: dict[str, int] = {}
        for o in self.orphans:
            by_verdict[o["verdict"]] = by_verdict.get(o["verdict"], 0) + 1
        return {"pointers": len(self.pointers), "by_ref_kind": dict(sorted(by_kind.items())),
                "by_method": dict(sorted(by_method.items())),
                "orphan_keywords": len(self.orphans),
                "orphans_by_verdict": dict(sorted(by_verdict.items())),
                "title_citations": len(self.title_citations),
                "llm_candidate_sentences": len(self.llm_sentences),
                "anaphora": sum(1 for p in self.pointers if p.anaphoric),
                "range_expanded": sum(1 for p in self.pointers
                                      if p.expansion_index is not None)}

    def as_dict(self) -> dict:
        return {
            "part": self.part,
            "counts": self.counts(),
            "pointers": [{
                "path": p.path, "parent_path": p.parent_path, "char_span": list(p.span),
                "text": p.text, "ref_kind": p.ref_kind, "unit": p.unit, "method": p.method,
                "family": p.family, "number": p.number, "item": p.item,
                "title_paren": p.title_paren, "context": p.context, "group_id": p.group_id,
                "anaphoric": p.anaphoric, "expansion_index": p.expansion_index,
                "legislation": p.legislation, "provision": p.provision, "notes": p.notes,
            } for p in self.pointers],
            "orphans": self.orphans,
            "title_citations": self.title_citations,
            "llm_candidate_sentences": self.llm_sentences,
        }


def text_nodes(root: Node) -> Iterator[Node]:
    for node in walk(root):
        if node.kind != "ref" and node.text:
            yield node


def _group_id(node: Node, part: str, index: int) -> str:
    """Unique across the corpus, not just within a part.

    Two parts both numbering a clause 9.2 would otherwise mint the same
    "g-9.2-1" for unrelated list phrases, and a query grouping refs by
    group_id would silently join citations from different schedules.
    """
    stem = node.label or node.path.rsplit("/", 1)[-1]
    return f"g-{part}-{stem}-{index}"


def _pointers_for(node: Node, citation: grammar.Citation, part: str,
                  group_index: int) -> list[Pointer]:
    """One pointer per cited target. A lone target owns the whole phrase; a
    list's members own their own numbers, exactly as SPEC 2.2 requires."""
    text = node.text or ""
    out: list[Pointer] = []
    real_members = [m for m in citation.members if not m.expanded]
    single = len(citation.members) == 1
    group = None if single else _group_id(node, part, group_index)

    if citation.anaphoric:
        return [Pointer(parent_path=node.path, part=part, span=citation.span,
                        text=text[citation.span[0]:citation.span[1]],
                        ref_kind=citation.ref_kind, unit=citation.unit,
                        method="anaphora", anaphoric=True, context=citation.context,
                        notes=list(citation.notes))]

    for member in citation.members:
        span = citation.span if single else member.span
        pointer = Pointer(
            parent_path=node.path, part=part, span=span,
            text=text[span[0]:span[1]],
            ref_kind=citation.ref_kind, unit=citation.unit, method=citation.method,
            family=citation.family, number=member.number, item=member.item,
            title_paren=member.title_paren or (citation.members[0].title_paren
                                               if single else None),
            context=citation.context, group_id=group,
            expansion_index=member.expansion_index,
            legislation=citation.legislation, notes=list(citation.notes))
        if citation.ref_kind == "legislation" and citation.legislation:
            if len(real_members) > 1 or citation.legislation.get("provision_unit"):
                pointer.provision = member.number
                pointer.number = member.number
            else:
                pointer.number = None
        if member.expanded:
            # SPEC 2.2: an implied interior member anchors to the whole range
            # phrase, the only ink that names it, and the implication is
            # recorded. Without this a reviewer, the audit judge and the UI all
            # see a ref whose span overlaps its siblings' and cannot tell an
            # expected overlap from a real span defect.
            pointer.notes = pointer.notes + [
                f"implied_range_member: {member.number} has no printed characters of "
                f"its own, so it anchors to {pointer.text!r}, the phrase that implies "
                f"it; overlap with its printed siblings' anchors is expected here "
                f"rather than a span defect, and the path ordinal keeps its id "
                f"distinct (JS1 1.3.10, series are inclusive)"]
        out.append(pointer)
    return out


def detect_part(part: str, root: Node, *, max_range: int = 60) -> PartDetection:
    """Every pointer, orphan and triage row in one part's tree."""
    found = PartDetection(part=part)
    order = 0
    for node in text_nodes(root):
        text = node.text or ""
        hits = legislation.find_legislation(text)
        citations = [legislation.as_citation(h) for h in hits]
        consumed = [h.span for h in hits]
        structural = grammar.find_citations(text, max_range=max_range, consumed=consumed)
        citations.extend(structural)
        consumed = [c.span for c in citations]
        consumed += [c.context["span"] for c in structural
                     if c.context and c.context["anaphoric"]]
        anaphora = grammar.find_anaphora(text, consumed)
        citations.extend(anaphora)
        citations.sort(key=lambda c: c.span)

        sentence_spans = grammar.sentences(text)
        for index, citation in enumerate(citations, start=1):
            for pointer in _pointers_for(node, citation, part, index):
                pointer.order = order
                pointer.sentence = _sentence_for(sentence_spans, pointer.span[0])
                found.pointers.append(pointer)
                order += 1

        orphans = grammar.find_orphans(text, [c.span for c in citations])
        for orphan in orphans:
            orphan["node_path"] = node.path
            found.orphans.append(orphan)
        for orphan in orphans:
            if orphan["verdict"] != "generic_prose":
                key = (node.path, tuple(orphan["sentence_span"]))
                if key not in {(s["node_path"], tuple(s["sentence_span"]))
                               for s in found.llm_sentences}:
                    found.llm_sentences.append(
                        {"node_path": node.path, "part": part,
                         "sentence_span": orphan["sentence_span"],
                         "sentence": orphan["sentence"],
                         "keywords": [orphan["keyword"]]})
                else:
                    for s in found.llm_sentences:
                        if (s["node_path"], tuple(s["sentence_span"])) == key:
                            s["keywords"].append(orphan["keyword"])

        if node.title:
            for citation in grammar.find_citations(node.title, max_range=max_range):
                found.title_citations.append(
                    {"node_path": node.path, "title": node.title,
                     "char_span": list(citation.span), "surface": citation.surface,
                     "ref_kind": citation.ref_kind,
                     "reason": "a citation inside a title has no text span to anchor to; "
                               "schemas.Node forbids ref children on a node with no text"})
    # Nodes that carry a title but no text never reach text_nodes, so scan them too.
    for node in walk(root):
        if node.kind == "ref" or node.text or not node.title:
            continue
        for citation in grammar.find_citations(node.title, max_range=max_range):
            found.title_citations.append(
                {"node_path": node.path, "title": node.title,
                 "char_span": list(citation.span), "surface": citation.surface,
                 "ref_kind": citation.ref_kind,
                 "reason": "a citation inside a title has no text span to anchor to; "
                           "schemas.Node forbids ref children on a node with no text"})
    return found


def _sentence_for(spans: list[tuple[int, int, str]], at: int) -> Optional[str]:
    for start, end, text in spans:
        if start <= at < end:
            return text
    return spans[-1][2] if spans else None
