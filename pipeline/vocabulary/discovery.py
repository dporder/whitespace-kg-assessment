"""The discovery rule, run independently over the whole corpus.

SPEC 2.3 and DESIGN tier 2: "legal drafting marks definitions by convention, a
quoted capitalised phrase followed by 'means' or 'has the meaning', or the
parenthetical form, so definition sites can be found without any list." This
module implements exactly that convention and knows nothing about Joint Schedule
1. Its output is kept apart from `declared.py`'s so stage 8 can diff the two
(`definitions_vs_provided`), which is the point of running it at all.

Three forms, all deterministic:

* **Prose.** `"Term" means ...` / `"Term" has the meaning ...`, the opening quote
  optional because this pack routinely omits it.
* **Table row.** The same convention split across two cells by the typesetting:
  a quoted term in the label cell whose value cell opens with the definitional
  verb. The row is read in reading order, which is a derived view of two stored
  cells and is never written back. Rows whose value cell does not carry the verb
  are *not* discovered, and that gap is a real measurement of how consistently
  the drafters followed their own convention rather than an artifact of a
  scanner that cannot see tables.
* **Parenthetical.** `... the EEA agreement ("EU References") which ...` mints a
  term. `Information and Communication Technology ("ICT")` does not: the
  parenthetical is an initialism of the phrase in front of it, so it is an alias
  of that phrase, not a new term. The initialism test in `text.py` is what
  separates the two, with no model and no list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from pipeline.schemas import Node
from pipeline.vocabulary import treeio
from pipeline.vocabulary.declared import (
    RawSite, definition_rows, pointer_for, split_trailing_alias,
)
from pipeline.vocabulary.text import (
    initials, is_capitalised_phrase, looks_like_term, term_key,
)

VERB = r"means\b|shall mean\b|has the meaning\b|shall have the meaning\b"

PROSE_FORM = re.compile(
    r"(?P<open>[\"“‘])?"
    r"(?P<term>[A-Z][A-Za-z0-9\-'’&/ ]{0,110}?)"
    r"(?P<close>[\"”’])"
    r"\s*(?:,\s*)?(?P<verb>" + VERB + r")")

# The definitional verb at the head of a definition cell.
VALUE_OPENS_WITH_VERB = re.compile(r"^\s*(?:" + VERB + r")")

QUOTED_PAREN = re.compile(
    r"\(\s*[\"“‘](?P<term>[^\"“”‘’()]{1,90}?)[\"”’]\s*\)")
BARE_PAREN = re.compile(r"\(\s*(?P<term>[A-Z][A-Za-z0-9&.\-]{1,24})\s*\)")

# How far back the alias test looks for the phrase an abbreviation stands for.
ALIAS_LOOKBACK_WORDS = 10


@dataclass
class AliasCandidate:
    """A parenthetical abbreviation introduced at first use."""
    alias: str
    phrase: str
    node_id: str
    node_path: str
    part: str
    attached_to: Optional[str] = None      # term key, once resolved


@dataclass
class DiscoveryResult:
    sites: list[RawSite] = field(default_factory=list)
    aliases: list[AliasCandidate] = field(default_factory=list)


def _abbreviated_phrase(field_text: str, end: int, abbreviation: str) -> str:
    """The phrase immediately before an offset that `abbreviation` stands for.

    Windows are tried shortest first, so `The Central Widget Office (CWO)` binds
    CWO to `Central Widget Office` and not to the sentence's opening `The`,
    which would then match no term. Quote marks are stripped per word, because
    the phrase often arrives still wearing the drafters' quotation marks:
    `"Central Buying Office" ("CBO")` must yield `Central Buying Office`.
    """
    letters = re.sub(r"[^A-Za-z]", "", abbreviation)
    if not letters or not letters.isupper() or len(letters) < 2:
        return ""
    words = [w.strip("()[]“”\"'’.,;:") for w in field_text[:end].split()]
    words = [w for w in words if w]
    tail = words[-ALIAS_LOOKBACK_WORDS:]
    for window in range(len(letters), min(len(letters) * 2 + 2, len(tail)) + 1):
        phrase = " ".join(tail[-window:]).strip()
        if phrase and is_capitalised_phrase(phrase) and initials(phrase) == letters:
            return phrase
    return ""


def _prose_sites(node: Node, part: str) -> list[RawSite]:
    out: list[RawSite] = []
    for value in (node.text, node.title):
        if not value:
            continue
        for m in PROSE_FORM.finditer(value):
            head, aliases = split_trailing_alias(m.group("term"))
            key = term_key(head)
            if not key or not looks_like_term(key) or not is_capitalised_phrase(key):
                continue
            out.append(RawSite(
                term=key, definition_node_id=node.id, scope="", aliases=aliases,
                pointer=pointer_for(value[m.end():]), part=part,
                term_node_id=node.id, term_node_path=node.path,
                definition_node_path=node.path, block_path=node.path,
                shape="prose", raw_term_text=m.group(0)))
    return out


def _row_sites(table: Node, part: str) -> list[RawSite]:
    """The convention as the typesetting splits it: quoted term cell, value
    cell opening with the definitional verb."""
    out: list[RawSite] = []
    for label, value in definition_rows(table):
        if not VALUE_OPENS_WITH_VERB.match(value.text or ""):
            continue
        head, aliases = split_trailing_alias(label.text or "")
        key = term_key(head)
        if not key or not looks_like_term(key) or not is_capitalised_phrase(key):
            continue
        out.append(RawSite(
            term=key, definition_node_id=value.id, scope="", aliases=aliases,
            pointer=pointer_for(value.text or ""), part=part,
            term_node_id=label.id, term_node_path=label.path,
            definition_node_path=value.path, block_path=table.path,
            shape="table_row", raw_term_text=label.text or ""))
    return out


def _parenthetical(node: Node, part: str) -> tuple[list[RawSite], list[AliasCandidate]]:
    sites: list[RawSite] = []
    aliases: list[AliasCandidate] = []
    for value in (node.text, node.title):
        if not value:
            continue
        for m in QUOTED_PAREN.finditer(value):
            key = term_key(m.group("term"))
            if not key or not looks_like_term(key):
                continue
            phrase = _abbreviated_phrase(value, m.start(), key)
            if phrase:
                aliases.append(AliasCandidate(alias=key, phrase=phrase, node_id=node.id,
                                              node_path=node.path, part=part))
                continue
            if not is_capitalised_phrase(key):
                continue
            sites.append(RawSite(
                term=key, definition_node_id=node.id, scope="", aliases=[],
                pointer=None, part=part, term_node_id=node.id,
                term_node_path=node.path, definition_node_path=node.path,
                block_path=node.path, shape="parenthetical",
                raw_term_text=m.group(0)))
        for m in BARE_PAREN.finditer(value):
            key = term_key(m.group("term"))
            if not key:
                continue
            phrase = _abbreviated_phrase(value, m.start(), key)
            if phrase:
                aliases.append(AliasCandidate(alias=key, phrase=phrase, node_id=node.id,
                                              node_path=node.path, part=part))
    return sites, aliases


def discover_part(part: Node) -> DiscoveryResult:
    pid = treeio.part_id(part)
    result = DiscoveryResult()
    # A definitions table's term cell prints the headword, and `_row_sites`
    # reads it as such. Letting the prose and parenthetical forms loose on the
    # same cell would read `"Central Buying Office" ("CBO")` a second time and
    # mint CBO as a term of its own instead of an alias of the phrase.
    headword_cells: set[str] = set()
    for node in treeio.walk(part):
        if node.kind == "table":
            headword_cells.update(label.id for label, _v in definition_rows(node))
    for node in treeio.walk(part):
        if node.kind == "ref" or node.id in headword_cells:
            continue
        if node.kind == "table":
            result.sites.extend(_row_sites(node, pid))
            continue
        result.sites.extend(_prose_sites(node, pid))
        sites, aliases = _parenthetical(node, pid)
        result.sites.extend(sites)
        result.aliases.extend(aliases)
    return result


def discover(trees: treeio.Trees) -> DiscoveryResult:
    out = DiscoveryResult()
    for _pid, part in trees.ordered():
        found = discover_part(part)
        out.sites.extend(found.sites)
        out.aliases.extend(found.aliases)
    return out
