"""Joining declared and discovered definition sites, and the vocabulary index.

The two lists stay separate all the way through `declared.py` and
`discovery.py`; this module is where they meet, and it records which list each
site came from in `DefinitionSite.source` (`declared`, `discovered`, `both`) so
stage 8 can diff them. It never merges a declared site into a discovered one, or
the other way round: a site is `both` only when the two passes named the same
term at the same definition node.

Scope, and what governs where. A site's scope is `document` or
`part:<part-id>`. Inside a part, a part-local site shadows a document-level one
for the same term (SPEC 2.3, DESIGN tier 1), and `TermUse.definition_used`
records the scope string of whichever site actually governed. A term whose only
definition is part-local *elsewhere* is not in scope at all: it is not matched
outside its own part, because a graph that recorded a defined-term use where no
definition governs would be asserting something the document does not say. The
run summary reports how many matches that rule suppressed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from pipeline.schemas import DefinitionSite
from pipeline.vocabulary.declared import DOCUMENT_SCOPE, RawSite
from pipeline.vocabulary.discovery import AliasCandidate


@dataclass
class MergedSite:
    raw: RawSite
    source: str                        # declared | discovered | both
    duplicate_of: Optional[str] = None  # path of the first site for this (term, scope)

    @property
    def term(self) -> str:
        return self.raw.term

    @property
    def scope(self) -> str:
        return self.raw.scope

    def to_schema(self) -> DefinitionSite:
        return DefinitionSite(
            term=self.raw.term,
            definition_node_id=self.raw.definition_node_id,
            source=self.source,
            scope=self.raw.scope,
            aliases=sorted(set(self.raw.aliases)),
            pointer=self.raw.pointer,
        )

    def provenance(self) -> dict:
        r = self.raw
        return {
            "term": r.term, "scope": r.scope, "source": self.source,
            "part": r.part, "shape": r.shape, "scope_source": r.scope_source,
            "definition_node_path": r.definition_node_path,
            "term_node_path": r.term_node_path, "block_path": r.block_path,
            "cue_path": r.cue_path, "cue_text": r.cue_text,
            "raw_term_text": r.raw_term_text, "aliases": sorted(set(r.aliases)),
            "pointer": r.pointer, "anomalies": r.anomalies,
            "duplicate_of": self.duplicate_of,
        }


def _scope_for_discovered(site: RawSite, document_scope_parts: set[str]) -> str:
    """A discovered definition governs its own part, unless it was found in the
    document-level definitions schedule, where it governs the document."""
    if site.part in document_scope_parts:
        return DOCUMENT_SCOPE
    return f"part:{site.part}"


def merge(declared_sites: list[RawSite], discovered: list[RawSite],
          aliases: Iterable[AliasCandidate],
          document_scope_parts: set[str]) -> tuple[list[MergedSite], list[AliasCandidate]]:
    """Merge the two passes. Order is reading order, declared first."""
    for site in discovered:
        if not site.scope:
            site.scope = _scope_for_discovered(site, document_scope_parts)

    by_key: dict[tuple[str, str], MergedSite] = {}
    out: list[MergedSite] = []
    for site in declared_sites:
        key = (site.term, site.definition_node_id)
        if key in by_key:
            continue                                   # same cell read twice
        merged = MergedSite(raw=site, source="declared")
        by_key[key] = merged
        out.append(merged)

    for site in discovered:
        key = (site.term, site.definition_node_id)
        existing = by_key.get(key)
        if existing is not None:
            existing.source = "both"
            existing.raw.aliases = list(dict.fromkeys(existing.raw.aliases + site.aliases))
            if existing.raw.pointer is None:
                existing.raw.pointer = site.pointer
            continue
        merged = MergedSite(raw=site, source="discovered")
        by_key[key] = merged
        out.append(merged)

    # Duplicate definitions of one term at one scope: kept, both, flagged.
    first_at: dict[tuple[str, str], MergedSite] = {}
    for m in out:
        k = (m.term, m.scope)
        if k in first_at:
            m.duplicate_of = first_at[k].raw.definition_node_path
        else:
            first_at[k] = m

    # Parenthetical abbreviations attach to the term they abbreviate.
    terms = defaultdict(list)
    for m in out:
        terms[m.term].append(m)
    unattached: list[AliasCandidate] = []
    for cand in aliases:
        targets = terms.get(cand.phrase)
        if not targets:
            unattached.append(cand)
            continue
        cand.attached_to = cand.phrase
        for m in targets:
            if cand.alias not in m.raw.aliases:
                m.raw.aliases.append(cand.alias)
    return out, unattached


# ------------------------------------------------------------- vocabulary


@dataclass
class Surface:
    """One matchable string and what it binds to inside one part's scope."""
    surface: str
    term: str
    is_alias: bool
    definition_used: str               # the governing site's scope string
    collides_with: list[str] = field(default_factory=list)


@dataclass
class PartVocabulary:
    part: str
    surfaces: dict[str, Surface]
    suppressed_out_of_scope: list[str] = field(default_factory=list)

    def ordered(self) -> list[Surface]:
        """Longest first, then alphabetical: the longest-match rule's order."""
        return sorted(self.surfaces.values(), key=lambda s: (-len(s.surface), s.surface))


def vocabulary_for(part: str, sites: list[MergedSite]) -> PartVocabulary:
    """The vocabulary that governs inside `part`.

    Document-level sites, overlaid by this part's own local sites. A surface
    that could bind to more than one term inside this scope is recorded as a
    collision on every term it could bind to; the matcher marks such matches
    `alias_collision` rather than picking one.
    """
    local_scope = f"part:{part}"
    governing: dict[str, str] = {}                     # term -> definition_used
    for m in sites:
        if m.scope == DOCUMENT_SCOPE:
            governing.setdefault(m.term, DOCUMENT_SCOPE)
    for m in sites:
        if m.scope == local_scope:
            governing[m.term] = local_scope            # part-local shadows document

    suppressed = sorted({m.term for m in sites
                         if m.term not in governing})

    binding: dict[str, set[str]] = defaultdict(set)
    alias_only: dict[str, bool] = {}
    for m in sites:
        if m.term not in governing:
            continue
        if m.scope not in (DOCUMENT_SCOPE, local_scope):
            continue
        binding[m.term].add(m.term)
        alias_only.setdefault(m.term, False)
        for alias in m.raw.aliases:
            binding[alias].add(m.term)
            alias_only.setdefault(alias, True)

    surfaces: dict[str, Surface] = {}
    for surface, terms in binding.items():
        ordered_terms = sorted(terms)
        primary = surface if surface in terms else ordered_terms[0]
        surfaces[surface] = Surface(
            surface=surface, term=primary,
            is_alias=alias_only.get(surface, True) and surface not in terms,
            definition_used=governing[primary],
            collides_with=ordered_terms if len(ordered_terms) > 1 else [])
    return PartVocabulary(part=part, surfaces=surfaces,
                          suppressed_out_of_scope=suppressed)
