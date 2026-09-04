"""Frozen data contracts. Owned by the orchestrator. Workers import, never edit.

One Node schema for everything that is ink on the page, differentiated by `kind`.
The per-kind table from handover/SPEC.md section 2.1 is enforced by a single
model validator rather than separate pydantic classes; the discrimination is in
the validation rules, the shape stays one schema, one walker, one id scheme.
Refs are nodes of kind "ref" annotating a character span of their parent leaf.
Term uses are USES_TERM edges, not nodes. See handover/SPEC.md section 2 for the
prose contract and DESIGN.md for the reasoning. Every stage validates its output
against these models before reporting done.

Identity helpers live here so there is exactly one implementation of the id
scheme. Hash input formats are part of the frozen contract:
    node id      = sha1("{document}|{version}|{path}")
    lineage_key  = sha1("{document}|{path}")
    content_hash = sha1(normalised own text)   text-bearing nodes only
Text normalisation exists for the hash key only, stored text is never altered:
NFC, CRLF to LF, trailing whitespace stripped per line, internal whitespace
runs collapsed to one space, leading and trailing whitespace stripped.
"""
from __future__ import annotations

import hashlib
import unicodedata
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator

Status = Literal["resolved", "ambiguous", "unresolved", "external"]

ANATOMY_KINDS = ("document", "part", "heading", "preamble", "clause",
                 "subclause", "item", "intro", "form_row", "table", "cell")
Kind = Literal["document", "part", "heading", "preamble", "clause", "subclause",
               "item", "intro", "form_row", "table", "cell", "ref"]

RefKind = Literal["clause", "schedule", "paragraph", "annex", "part",
                  "definition", "legislation", "unknown"]
ScopeRule = Literal["js1_1.3.8", "js1_1.3.9", "title_paren", "same_part", "none"]
Resolver = Literal["grammar", "scope", "llm", "human"]
AmbiguityKind = Literal["none", "sentence_initial", "heading", "typo_dense", "alias_collision"]

EdgeType = Literal["CONTAINS", "NEXT", "RESOLVES_TO", "CANDIDATE", "USES_TERM",
                   "DEFINED_IN", "ABOUT", "DEFINED_USING", "CONCEPT_REL",
                   "ASSOCIATED_TERM", "SUPERSEDES"]


def node_id(document: str, version: str, path: str) -> str:
    """This version's instance of the provision at `path`."""
    return hashlib.sha1(f"{document}|{version}|{path}".encode()).hexdigest()


def lineage_key(document: str, path: str) -> str:
    """The provision as a thing that persists across versions."""
    return hashlib.sha1(f"{document}|{path}".encode()).hexdigest()


def normalise_for_hash(text: str) -> str:
    """Key normalisation only. Stored text is never altered anywhere."""
    t = unicodedata.normalize("NFC", text).replace("\r\n", "\n")
    lines = [" ".join(line.split()) for line in t.split("\n")]
    return "\n".join(lines).strip()


def content_hash(text: str) -> str:
    return hashlib.sha1(normalise_for_hash(text).encode()).hexdigest()


class BBox(BaseModel):
    page: int                      # 1-based absolute PDF page
    bbox: tuple[float, float, float, float]   # x0, y0, x1, y1, PyMuPDF points, origin top-left


class Candidate(BaseModel):
    path: str
    score: float
    reason: Optional[str] = None


# Fields legal only on one kind. Anything else carrying them fails validation.
_DOC_ONLY = ("version_label", "source_file", "source_sha256", "file_created",
             "file_author", "ingested_at", "pipeline_version", "page_routes",
             "custodian", "access_label")
_PART_ONLY = ("part_family", "template_version")
_TABLE_ONLY = ("n_rows", "n_cols")
_CELL_ONLY = ("row", "col", "cell_role", "role_confidence")
_REF_ONLY = ("char_span", "group_id", "ref_kind", "scope_rule", "status",
             "target_path", "confidence", "resolver")


class Node(BaseModel):
    """One schema for everything that is ink on the page.

    The branch-or-leaf rule: a node has anatomy children or it has text, never
    both, at any depth. Intro children carry a container's lead-in words and
    take the path segment `intro`. Ref children are annotations of a text
    span, not anatomy, so any text-bearing node (clause, subclause, item,
    intro, cell, a leaf heading or preamble) may hold text and ref children at
    once. The branch-or-leaf rule quantifies over anatomy kinds only.
    """
    id: str                        # sha1(document|version|path), this version's instance
    lineage_key: str               # sha1(document|path), stable across versions
    content_hash: Optional[str] = None   # sha1 of normalised own text, text-bearing nodes only
    path: str                      # core-terms/3/3.1/3.1.2/a ; refs append /ref@start-end
    kind: Kind
    unit_label: Optional[str] = None     # Clause | Paragraph ... from interpretation clause or profile
    unit_label_source: Optional[Literal["document", "profile"]] = None
    citable: bool = True           # False for intro and ref nodes
    label: Optional[str] = None    # "3.1.2" or "(a)"; queryable lookup key, never identity
    title: Optional[str] = None    # headings and parts only
    text: Optional[str] = None     # own words only; refs hold the pointing words only
    page_start: int
    page_end: int
    printed_page: Optional[str] = None   # from the part's own footer, restarts per part
    bboxes_own: list[BBox] = Field(default_factory=list)
    bboxes_extent: list[BBox] = Field(default_factory=list)
    order: int                     # preorder position within the part, reading order
    children: list["Node"] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    batch_id: Optional[str] = None

    # -- document kind only (root metadata; None elsewhere) -------------------
    version_label: Optional[str] = None
    source_file: Optional[str] = None
    source_sha256: Optional[str] = None
    file_created: Optional[str] = None
    file_author: Optional[str] = None
    ingested_at: Optional[str] = None
    pipeline_version: Optional[str] = None
    page_routes: Optional[dict[str, str]] = None   # page -> extraction route taken
    custodian: Optional[str] = None
    access_label: Optional[str] = None

    # -- part kind only --------------------------------------------------------
    part_family: Optional[Literal["core", "award-form", "framework-schedule",
                                  "joint-schedule", "call-off-schedule"]] = None
    template_version: Optional[str] = None          # from the part's footer

    # -- table kind only -------------------------------------------------------
    n_rows: Optional[int] = None
    n_cols: Optional[int] = None

    # -- cell kind only --------------------------------------------------------
    row: Optional[int] = None
    col: Optional[int] = None
    cell_role: Optional[Literal["label", "value", "header"]] = None
    role_confidence: Optional[float] = None         # how plausible the physical role is

    # -- ref kind only ---------------------------------------------------------
    char_span: Optional[tuple[int, int]] = None     # offsets into the PARENT node's text
    group_id: Optional[str] = None                  # shared by refs split from one list phrase
    ref_kind: Optional[RefKind] = None
    scope_rule: Optional[ScopeRule] = None
    status: Optional[Status] = None
    target_path: Optional[str] = None               # never minted: unresolved refs keep None
    candidates: list[Candidate] = Field(default_factory=list)
    confidence: Optional[float] = None              # calibrated post-hoc; see EVALUATION.md layer 5
    resolver: Optional[Resolver] = None

    def _forbid(self, fields: tuple[str, ...], group: str) -> None:
        for f in fields:
            if getattr(self, f) is not None:
                raise ValueError(f"{self.kind} node {self.path} carries {group}-only field {f}")

    @model_validator(mode="after")
    def _kind_rules(self) -> "Node":
        anatomy_children = [c for c in self.children if c.kind != "ref"]
        ref_children = [c for c in self.children if c.kind == "ref"]

        # -- fields scoped to a kind never appear on another kind --------------
        if self.kind != "document":
            self._forbid(_DOC_ONLY, "document")
        if self.kind != "part":
            self._forbid(_PART_ONLY, "part")
        if self.kind != "table":
            self._forbid(_TABLE_ONLY, "table")
        if self.kind != "cell":
            self._forbid(_CELL_ONLY, "cell")
        if self.kind != "ref":
            self._forbid(_REF_ONLY, "ref")
            if self.candidates:
                raise ValueError(f"{self.kind} node {self.path} carries ref-only candidates")
        if self.kind not in ("heading", "part"):
            if self.title is not None:
                raise ValueError(f"{self.kind} node {self.path} carries a title")

        if self.kind == "ref":
            for f in ("char_span", "ref_kind", "scope_rule", "status", "resolver"):
                if getattr(self, f) is None:
                    raise ValueError(f"ref node {self.path} missing {f}")
            if self.text is None:
                raise ValueError(f"ref node {self.path} must hold its pointing words")
            if self.citable:
                raise ValueError(f"ref node {self.path} must be citable=False")
            if self.children:
                raise ValueError(f"ref node {self.path} cannot have children")
            if self.status == "resolved" and not self.target_path:
                raise ValueError(f"resolved ref {self.path} has no target_path")
            if self.target_path and self.status == "unresolved":
                raise ValueError(f"unresolved ref {self.path} carries a target_path")
            return self

        # -- anatomy kinds ------------------------------------------------------
        # branch-or-leaf: anatomy children XOR text
        if anatomy_children and self.text is not None:
            raise ValueError(f"{self.kind} node {self.path} has both text and anatomy children")
        if ref_children and self.text is None:
            raise ValueError(f"{self.kind} node {self.path} has ref children but no text to anchor them")

        if self.kind == "intro":
            if self.citable:
                raise ValueError(f"intro node {self.path} must be citable=False")
            if anatomy_children:
                raise ValueError(f"intro node {self.path} cannot have anatomy children")
            if not self.text:
                raise ValueError(f"intro node {self.path} must carry text")
        if self.kind == "cell":
            if self.text is None:
                raise ValueError(f"cell node {self.path} must carry text (empty string for blank ink)")
            if anatomy_children:
                raise ValueError(f"cell node {self.path} cannot have anatomy children")
            for f in ("row", "col", "cell_role"):
                if getattr(self, f) is None:
                    raise ValueError(f"cell node {self.path} missing {f}")
        if self.kind in ("document", "part", "form_row", "table") and self.text is not None:
            raise ValueError(f"{self.kind} node {self.path} must not carry text")
        if self.kind == "table":
            if self.n_rows is None or self.n_cols is None:
                raise ValueError(f"table node {self.path} missing n_rows/n_cols")
        if self.kind in ("form_row", "table"):
            bad = [c.kind for c in anatomy_children if c.kind != "cell"]
            if bad:
                raise ValueError(f"{self.kind} node {self.path} has non-cell children {bad}")
        if self.kind == "document":
            bad = [c.kind for c in anatomy_children if c.kind != "part"]
            if bad:
                raise ValueError(f"document node {self.path} has non-part children {bad}")
        return self


class RefsFile(BaseModel):
    """Stage 3 output, output/<run>/refs/<part>.json. A flat list: stage 2
    trees carry no ref children, stage 7 attaches refs to their parents by
    path. A ref's path is its parent's path plus /ref@<start>-<end>."""
    part: str
    refs: list[Node]

    @model_validator(mode="after")
    def _all_refs(self) -> "RefsFile":
        for n in self.refs:
            if n.kind != "ref":
                raise ValueError(f"refs file for {self.part} contains non-ref node {n.path}")
        return self


class Legislation(BaseModel):
    """Normalised legislation referent. Normalisation mints the key only, the
    pointing words on every citing ref stay untouched."""
    key: str                       # legislation/bribery-act-2010[/section/55]
    title: str                     # parenthesised qualifiers belong to the title
    year: int
    instrument_kind: Literal["act", "regulations", "eu_regulation"]
    provision: Optional[str] = None   # e.g. "section/55" when the citation points inside


class DefinitionSite(BaseModel):
    term: str
    definition_node_id: str
    source: Literal["declared", "discovered", "both"]
    scope: str                     # "document" or "part:<part-id>" (local definitions shadow JS1)
    aliases: list[str] = Field(default_factory=list)   # parenthetical abbreviations at first use
    pointer: Optional[str] = None  # when the definition delegates, e.g. to Schedule 6


class TermUse(BaseModel):
    """Becomes a USES_TERM edge. An edge, not a node: its target always exists."""
    term: str
    node_id: str
    char_span: tuple[int, int]     # offsets into the node's text (its title for heading matches)
    status: Literal["confident", "ambiguous"]
    ambiguity_kind: AmbiguityKind = "none"
    method: Literal["exact_longest", "llm", "human"]
    definition_used: Optional[str] = None   # scope of the governing DefinitionSite,
                                            # "document" or "part:<part-id>"


class ConceptRelation(BaseModel):
    src: str
    label: str
    dst: str


class Concept(BaseModel):
    id: str
    label: str
    scope_path: str
    member_node_ids: list[str]
    relations: list[ConceptRelation] = Field(default_factory=list)
    llm_derived: bool = True
    confidence: float


class EmbeddingRecord(BaseModel):
    node_id: str
    level: Literal["leaf_text", "leaf_window", "subtree_text", "summary"]
    text: str
    vector_ref: str                # path into output/, vectors never live on graph nodes
    llm_derived: bool = False      # True for summary level


class GraphEdge(BaseModel):
    """One JSONL row under output/<run>/graph/edges.jsonl, the loader's input.
    The MERGE key is (type, src, dst) plus, where several edges legally join
    the same pair, the discriminating prop: char_span for USES_TERM, nothing
    else needs one. src and dst are node ids or referent keys (Term.name,
    Legislation.key, Concept.id)."""
    type: EdgeType
    src: str
    dst: str
    props: dict = Field(default_factory=dict)
    batch_id: str
