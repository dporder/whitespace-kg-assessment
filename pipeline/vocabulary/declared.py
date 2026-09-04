"""Declared ingestion: the definitions the document itself sets out.

Two shapes, both deterministic, both read straight off the stage 2 tree:

1. **Definition tables.** Joint Schedule 1 and several call-off schedules print
   definitions as a two-column table, the quoted term in the label cell and the
   definition in the value cell. Stage 1 parses those by box geometry, so by the
   time they reach here they are `table` nodes of `cell` children carrying `row`,
   `col` and `cell_role`.
2. **Prose definition blocks.** A block introduced by an interpretation cue whose
   members read `"Term" means ...`. Those are found by the same grammar the
   discovery rule uses (`pipeline/vocabulary/discovery.py`); what makes them
   *declared* rather than merely discovered is that they sit inside a block the
   document introduced as a definitions block.

**Scope comes from the document, not from a hardcoded part id.** The block's own
lead-in says how far its definitions reach. Joint Schedule 1 paragraph 1.4 opens
"In each Contract, unless the context otherwise requires, the following words
shall have the following meanings", which is document scope. Call-Off Schedule 9
paragraph 1.1 opens "In this Schedule, the following words shall have the
following meanings and they shall supplement Joint Schedule 1 (Definitions)",
which is `part:<part-id>` and shadows the document-level list inside that part.
Only when no cue is found at all does the part's identity decide, and that
fallback is recorded on the site so the report can show how often it fired.

**Nothing is repaired.** 206 term cells across the batch-B2 pages print a closing
quote with no opening one, and a dozen have lost their first letter in the source
itself (`nsurances` for Insurances, `ncorporated Terms` for Incorporated Terms,
verified against the PDF at 4x). The key minted here strips quote marks and
collapses the whitespace a wrapped cell introduces, and changes nothing else, so
`nsurances` stays `nsurances`. The defect is recorded as an anomaly beside the
site.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from pipeline.schemas import Node
from pipeline.vocabulary import treeio
from pipeline.vocabulary.text import (
    collapse_ws, is_initialism_of, looks_like_term, quote_shape, strip_quotes, term_key,
)

DOCUMENT_SCOPE = "document"

# Lead-ins that introduce a block of definitions. The first three come straight
# from config.HIERARCHY_PROFILES[...]["interpretation_cues"]; the rest are the
# shapes this family actually prints above its definition tables.
CUE_PATTERNS = [
    re.compile(r"the following words shall have the following meanings", re.I),
    re.compile(r"the following (?:defined terms|definitions) (?:shall )?apply", re.I),
    re.compile(r"\bIn this (?:Schedule|Part|Annex|Appendix)\b"),
    re.compile(r"\bIn each Contract\b"),
    re.compile(r"\bIn this Contract\b"),
    re.compile(r"unless the context otherwise requires", re.I),
    re.compile(r"\bthe following (?:words|terms|expressions) (?:shall )?(?:have|mean)", re.I),
]

# Which way a cue scopes its block.
_PART_LOCAL_CUE = re.compile(r"\bIn this (?:Schedule|Part|Annex|Appendix)\b")
_DOCUMENT_CUE = re.compile(r"\bIn (?:each|this) Contract\b|\bIn the Framework\b")

# A definition that delegates rather than states.
DELEGATION = re.compile(
    r"(?:has|have|shall have)\s+the\s+meaning\s+(?:given|set\s+out|ascribed)?"
    r"[^.;]{0,40}?\bin\b"
    r"|as\s+(?:defined|described|set\s+out|specified)\s+in\b"
    r"|the\s+meaning\s+given\s+to\s+it\s+in\b",
    re.I)

# What a delegation points at.
POINTER_TARGET = re.compile(
    r"(?:(?:Framework|Joint|Call-Off)\s+)?"
    r"(?:Schedule|Clause|Paragraph|Annex|Appendix|Part|Section)"
    r"\s+\d+[A-Za-z]?(?:\.\d+)*")

# A trailing parenthetical on a term cell, the alias convention.
TRAILING_PAREN = re.compile(r"^(?P<head>.*?)\s*\(\s*(?P<paren>[^()]{1,60}?)\s*\)\s*$", re.S)


@dataclass
class RawSite:
    """One definition site plus the provenance `DefinitionSite` has no room for.

    The schema fields are `term`, `definition_node_id`, `scope`, `aliases` and
    `pointer`; everything else here is written to the side file so a reviewer can
    see which block minted a term and what was wrong with the ink.
    """
    term: str
    definition_node_id: str
    scope: str
    aliases: list[str] = field(default_factory=list)
    pointer: Optional[str] = None
    # provenance, side file only
    part: str = ""
    term_node_id: Optional[str] = None
    term_node_path: Optional[str] = None
    definition_node_path: str = ""
    block_path: Optional[str] = None
    cue_path: Optional[str] = None
    cue_text: Optional[str] = None
    scope_source: str = "cue"          # cue | part_identity | block_default
    shape: str = "table"               # table | prose
    raw_term_text: str = ""
    anomalies: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ cues


def _cue_match(text: str) -> Optional[str]:
    for pat in CUE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def _scope_from_cue(cue_sentence: str, part: str) -> tuple[str, str]:
    """(scope, scope_source) for a block whose lead-in reads `cue_sentence`.

    The whole lead-in sentence is classified, not just the phrase that matched
    a cue pattern. Joint Schedule 1 opens "In each Contract, unless the context
    otherwise requires, the following words shall have the following meanings",
    and classifying only the trailing clause would lose the words that say how
    far the block reaches.
    """
    if _PART_LOCAL_CUE.search(cue_sentence):
        return f"part:{part}", "cue"
    if _DOCUMENT_CUE.search(cue_sentence):
        return DOCUMENT_SCOPE, "cue"
    return f"part:{part}", "block_default"


@dataclass
class Cue:
    node: Node
    marker: str          # the phrase that matched a cue pattern
    sentence: str        # the whole field it was found in, which is what scopes


def governing_cue(part: Node, target: Node) -> Optional[Cue]:
    """The nearest definitions lead-in printed before `target` inside `part`.

    Searched first within the target's own top-level section, then across the
    whole part, so a table sitting under its own paragraph finds that
    paragraph's lead-in rather than one from three sections earlier.
    """
    sections = {s.path: s for s in treeio.sections(part)}
    home = treeio.section_of(part).get(target.id)

    def scan(nodes: Iterable[Node]) -> Optional[Cue]:
        best: Optional[Cue] = None
        best_order = -1
        for n in nodes:
            if n.kind == "ref" or n.order >= target.order:
                continue
            for value in (n.text, n.title):
                if not value:
                    continue
                marker = _cue_match(value)
                if marker and n.order > best_order:
                    best, best_order = Cue(n, marker, value), n.order
        return best

    if home and home in sections:
        found = scan(treeio.walk(sections[home].node))
        if found is not None:
            return found
    return scan(treeio.walk(part))


def part_is_document_definitions(part: Node, batches: dict) -> bool:
    """Is this the document-level definitions schedule?

    Config-driven: the batch whose genre is "definitions" names the part. The
    title fallback covers a part outside the configured batches.
    """
    pid = treeio.part_id(part)
    for spec in batches.values():
        if spec.get("genre") == "definitions" and spec.get("part") == pid:
            return True
    title = (part.title or "").lower()
    return "(definitions)" in title


# --------------------------------------------------------------- pointers


def pointer_for(text: str) -> Optional[str]:
    """The target of a delegating definition, e.g. "Schedule 6"."""
    m = DELEGATION.search(text)
    if not m:
        return None
    target = POINTER_TARGET.search(text, m.start())
    return collapse_ws(target.group(0)) if target else None


# ----------------------------------------------------------------- aliases


def split_trailing_alias(raw: str) -> tuple[str, list[str]]:
    """Split a printed term into its term text and any parenthetical alias.

    `"Central Buying Office" ("CBO")` -> (`"Central Buying Office"`, ["CBO"]).
    A trailing parenthetical is taken as an alias only when it is quoted or is
    an initialism of the term in front of it, so a qualifier in brackets is not
    mistaken for an abbreviation.
    """
    m = TRAILING_PAREN.match(raw.strip())
    if not m:
        return raw, []
    head, paren = m.group("head"), m.group("paren")
    alias = term_key(paren)
    was_quoted = bool(paren.strip()) and paren.strip()[0] in "\"“‘«„"
    head_key = term_key(head)
    if not alias or not looks_like_term(alias) or not head_key:
        return raw, []
    if was_quoted or is_initialism_of(alias, head_key):
        return head, [alias]
    return raw, []


# ------------------------------------------------------------ table blocks


def _rows(table: Node) -> dict[int, list[Node]]:
    rows: dict[int, list[Node]] = {}
    for cell in treeio.anatomy_children(table):
        if cell.kind != "cell" or cell.row is None:
            continue
        rows.setdefault(cell.row, []).append(cell)
    for cells in rows.values():
        cells.sort(key=lambda c: (c.col if c.col is not None else 0, c.order))
    return rows


def _label_and_value(cells: list[Node]) -> tuple[Optional[Node], Optional[Node]]:
    labels = [c for c in cells if c.cell_role == "label"]
    values = [c for c in cells if c.cell_role == "value"]
    if labels and values:
        return labels[0], values[0]
    if len(cells) >= 2 and all(c.cell_role != "header" for c in cells):
        return cells[0], cells[1]
    return None, None


def definition_rows(table: Node) -> list[tuple[Node, Node]]:
    """(label cell, value cell) for rows whose label reads as a term."""
    out = []
    for _row, cells in sorted(_rows(table).items()):
        label, value = _label_and_value(cells)
        if label is None or value is None:
            continue
        head, _aliases = split_trailing_alias(label.text or "")
        key = term_key(head)
        if key and looks_like_term(key) and (value.text or "").strip():
            out.append((label, value))
    return out


def is_definitions_table(table: Node, under_cue: bool) -> bool:
    """A table is a definitions table when most of its rows read as term rows.

    Quote marks are the drafters' own signal, so a table whose labels are mostly
    quoted qualifies on its own. A table without them qualifies only when a
    definitions lead-in introduced it, which is what keeps an ordinary two-column
    table of, say, milestones out of the vocabulary.
    """
    rows = _rows(table)
    if len(rows) < 2:
        return False
    defn = definition_rows(table)
    if len(defn) < 2 or len(defn) / len(rows) < 0.6:
        return False
    quoted = sum(1 for label, _v in defn if quote_shape(label.text or "") != "none")
    return under_cue or quoted / len(defn) >= 0.5


# ------------------------------------------------------------ prose blocks

PROSE_DEFINITION = re.compile(
    r"[\"“‘]?(?P<term>[A-Z][^\"“”‘’\n]{0,110}?)[\"”’]"
    r"\s*(?:,\s*)?(?P<verb>means\b|has the meaning\b|shall mean\b)")


def prose_definitions(node: Node) -> list[tuple[str, str]]:
    """(printed term, matched verb) for `"Term" means ...` inside one node."""
    out = []
    for value in (node.text, node.title):
        if not value:
            continue
        for m in PROSE_DEFINITION.finditer(value):
            key = term_key(m.group("term"))
            if key and looks_like_term(key):
                out.append((m.group("term"), m.group("verb")))
    return out


# ------------------------------------------------------------------ ingest


def ingest_part(part: Node, batches: dict) -> list[RawSite]:
    """Every declared definition site in one part, in reading order."""
    sites: list[RawSite] = []
    pid = treeio.part_id(part)
    doc_level_part = part_is_document_definitions(part, batches)

    for node in treeio.walk(part):
        if node.kind != "table":
            continue
        cue = governing_cue(part, node)
        if not is_definitions_table(node, under_cue=cue is not None):
            continue
        if cue is not None:
            scope, scope_source = _scope_from_cue(cue.sentence, pid)
            if scope_source == "block_default" and doc_level_part:
                scope, scope_source = DOCUMENT_SCOPE, "part_identity"
        elif doc_level_part:
            scope, scope_source = DOCUMENT_SCOPE, "part_identity"
        else:
            scope, scope_source = f"part:{pid}", "part_identity"

        for label, value in definition_rows(node):
            raw = label.text or ""
            head, aliases = split_trailing_alias(raw)
            key = term_key(head)
            anomalies = []
            shape = quote_shape(raw)
            if shape == "closing_only":
                anomalies.append("term_cell_closing_quote_without_opening")
            elif shape == "none":
                anomalies.append("term_cell_unquoted")
            if key and key[0].islower():
                anomalies.append("term_cell_starts_lowercase_"
                                 "first_letter_absent_in_source")
            sites.append(RawSite(
                term=key, definition_node_id=value.id, scope=scope, aliases=aliases,
                pointer=pointer_for(value.text or ""), part=pid,
                term_node_id=label.id, term_node_path=label.path,
                definition_node_path=value.path, block_path=node.path,
                cue_path=cue.node.path if cue else None,
                cue_text=cue.sentence if cue else None,
                scope_source=scope_source, shape="table", raw_term_text=raw,
                anomalies=anomalies))

    # Prose blocks: a `"Term" means ...` sentence inside a node a definitions
    # lead-in introduced. The same sentence is also found by the discovery rule;
    # what this pass adds is that the document declared it.
    for node in treeio.walk(part):
        if node.kind == "table" or node.kind == "ref":
            continue
        found = prose_definitions(node)
        if not found:
            continue
        own_marker = _cue_match(node.text or "") or _cue_match(node.title or "")
        cue = (Cue(node, own_marker, node.text or node.title or "") if own_marker
               else governing_cue(part, node))
        if cue is None:
            continue                                   # discovered only, not declared
        scope, scope_source = _scope_from_cue(cue.sentence, pid)
        if doc_level_part and scope_source == "block_default":
            scope, scope_source = DOCUMENT_SCOPE, "part_identity"
        for raw_term, _verb in found:
            head, aliases = split_trailing_alias(raw_term)
            key = term_key(head)
            sites.append(RawSite(
                term=key, definition_node_id=node.id, scope=scope, aliases=aliases,
                pointer=pointer_for(node.text or ""), part=pid,
                term_node_id=node.id, term_node_path=node.path,
                definition_node_path=node.path, block_path=node.path,
                cue_path=cue.node.path, cue_text=cue.sentence,
                scope_source=scope_source, shape="prose", raw_term_text=raw_term,
                anomalies=[]))
    return sites


def ingest(trees: treeio.Trees, batches: dict) -> list[RawSite]:
    out: list[RawSite] = []
    for _pid, part in trees.ordered():
        out.extend(ingest_part(part, batches))
    return out
