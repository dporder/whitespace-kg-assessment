"""The matcher. Case sensitive, longest match wins, overlaps forbidden.

SPEC 2.3's three rules plus the typed ambiguity that comes out of them:

* **Case sensitive.** A `Default` is not a `default`; the capital is the whole
  signal, which is also why the typo-density stratum exists.
* **Longest match wins.** `Call-Off Contract` beats `Contract`, because the
  longer string is the more specific term and a shorter term inside a longer
  match is a fragment of it, not an independent use.
* **Overlaps forbidden.** Selection is a deterministic greedy sweep, longest
  first, ties by earlier start then by surface, so two runs cannot disagree.
* **Aliases equal to full forms.** `CBO` matches exactly as
  `Central Buying Office` does, and the record carries the canonical term with
  the alias's span (SPEC 2.3, DESIGN tier 2).

Word boundaries are required on both sides, so `Contract` does not match inside
`Contracts`. The plural is reached instead through the document's own
stipulation: Joint Schedule 1 paragraph 1.3.1 says "the singular includes the
plural and vice versa", so `sites.inflections` expands every printed surface
with its simple s/es forms and `Contracts` matches as a surface of its own,
carrying the canonical term with the inflected span exactly as an alias does.
A printed surface always outranks an inflected one for the same string.

Ambiguity is typed, and when more than one kind applies the record keeps the one
that most changes what a checker would have to decide, in this order:

    alias_collision > typo_dense > heading > sentence_initial

An alias that could bind to two terms is a question about *which term*, which no
other kind answers. A typo-dense section makes the capital itself untrustworthy
in both directions, which subsumes the positional kinds. Heading position is a
stronger signal than sentence-initial position, because a heading is capitalised
throughout by convention. Every applicable kind is kept on the routing record so
nothing is lost, and only the schema field is single-valued.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from pipeline.schemas import Node, TermUse
from pipeline.vocabulary import treeio
from pipeline.vocabulary.sites import MergedSite, PartVocabulary, Surface
from pipeline.vocabulary.text import sentence_initial

WORD_CHAR = re.compile(r"[A-Za-z0-9]")

# The headword of a definition is not a use of the term it defines.
HEADWORD_TAIL = re.compile(
    r"^[\"”’]?\s*(?:,\s*)?(?:means\b|shall mean\b|has the meaning\b"
    r"|shall have the meaning\b)")

AMBIGUITY_PRECEDENCE = ("alias_collision", "typo_dense", "heading", "sentence_initial")


@dataclass
class Match:
    """One term use plus everything the router and the audit need.

    `TermUse` carries the six fields the graph needs; the rest travels in the
    side files so a reviewer sees the sentence, not just an offset.
    """
    term: str
    surface: str
    node_id: str
    node_path: str
    part: str
    section_path: str
    field_name: str                    # "text" or "title"
    span: tuple[int, int]
    status: str
    ambiguity_kind: str
    kinds: list[str] = field(default_factory=list)
    definition_used: Optional[str] = None
    is_alias: bool = False
    is_inflected: bool = False
    collides_with: list[str] = field(default_factory=list)
    order: int = 0
    page_start: int = 0
    sentence: str = ""
    # "exact_longest" until a routed check settles it, then "llm" (or "human",
    # when a review decision is folded back in). SPEC 2.3's `method`.
    method: str = "exact_longest"

    def to_schema(self) -> TermUse:
        return TermUse(term=self.term, node_id=self.node_id, char_span=self.span,
                       status=self.status, ambiguity_kind=self.ambiguity_kind,
                       method=self.method, definition_used=self.definition_used)

    def as_dict(self) -> dict:
        return {
            "term": self.term, "surface": self.surface, "node_id": self.node_id,
            "path": self.node_path, "part": self.part,
            "section_path": self.section_path, "field": self.field_name,
            "char_span": list(self.span), "status": self.status,
            "ambiguity_kind": self.ambiguity_kind, "ambiguity_kinds": self.kinds,
            "definition_used": self.definition_used, "matched_alias": self.is_alias,
            "matched_inflection": self.is_inflected,
            "collides_with": self.collides_with, "order": self.order,
            "page_start": self.page_start, "method": self.method,
            "sentence": self.sentence,
        }


def _boundary_ok(field_text: str, start: int, end: int) -> bool:
    before = field_text[start - 1] if start > 0 else ""
    after = field_text[end] if end < len(field_text) else ""
    return not (WORD_CHAR.match(before) if before else False) and \
           not (WORD_CHAR.match(after) if after else False)


def candidates(field_text: str, vocab: PartVocabulary) -> list[tuple[int, int, Surface]]:
    """Every boundary-respecting occurrence of every surface. Unfiltered."""
    found: list[tuple[int, int, Surface]] = []
    for surface in vocab.ordered():
        needle = surface.surface
        if not needle:
            continue
        start = field_text.find(needle)
        while start != -1:
            end = start + len(needle)
            if _boundary_ok(field_text, start, end):
                found.append((start, end, surface))
            start = field_text.find(needle, start + 1)
    return found


def select(found: Iterable[tuple[int, int, Surface]]) -> list[tuple[int, int, Surface]]:
    """Longest match wins, overlaps forbidden. Deterministic."""
    ordered = sorted(found, key=lambda t: (-(t[1] - t[0]), t[0], t[2].surface))
    taken: list[tuple[int, int, Surface]] = []
    for start, end, surface in ordered:
        if any(start < t_end and t_start < end for t_start, t_end, _s in taken):
            continue
        taken.append((start, end, surface))
    taken.sort(key=lambda t: (t[0], t[1]))
    return taken


def _label_cells_of_definitions(sites: list[MergedSite]) -> set[str]:
    """Nodes that print a term as a definition's headword, not as a use."""
    return {s.raw.term_node_id for s in sites
            if s.raw.shape in ("table", "table_row") and s.raw.term_node_id}


def match_part(part: Node, vocab: PartVocabulary, sites: list[MergedSite],
               typo_dense: callable, section_of: dict[str, str]) -> list[Match]:
    """Every term use inside one part, in reading order."""
    headword_nodes = _label_cells_of_definitions(sites)
    out: list[Match] = []
    for node in treeio.walk(part):
        if node.kind == "ref" or node.id in headword_nodes:
            continue
        for field_name, value in treeio.own_texts(node):
            for start, end, surface in select(candidates(value, vocab)):
                if HEADWORD_TAIL.match(value[end:end + 40]):
                    continue                      # the headword of a definition
                kinds: list[str] = []
                if surface.collides_with:
                    kinds.append("alias_collision")
                if typo_dense(node.id):
                    kinds.append("typo_dense")
                if field_name == "title":
                    kinds.append("heading")
                if sentence_initial(value, start):
                    kinds.append("sentence_initial")
                kind = next((k for k in AMBIGUITY_PRECEDENCE if k in kinds), "none")
                # Which definition governs depends on where the use stands,
                # not on which part it is in: a sub-part definition shadows a
                # part-level one (SPEC 2.3).
                definition_used = (vocab.governing_scope(surface.term, node.path)
                                   or surface.definition_used)
                out.append(Match(
                    term=surface.term, surface=surface.surface, node_id=node.id,
                    node_path=node.path, part=vocab.part,
                    section_path=section_of.get(node.id, part.path),
                    field_name=field_name, span=(start, end),
                    status="ambiguous" if kinds else "confident",
                    ambiguity_kind=kind, kinds=kinds,
                    definition_used=definition_used,
                    is_alias=surface.is_alias, is_inflected=surface.is_inflected,
                    collides_with=surface.collides_with, order=node.order,
                    page_start=node.page_start, sentence=value))
    out.sort(key=lambda m: (m.order, m.field_name, m.span[0], m.term))
    return out
