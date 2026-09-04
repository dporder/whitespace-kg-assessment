"""Frozen data contracts. Owned by the orchestrator. Workers import, never edit.

One Node schema for everything that is ink on the page, differentiated by `kind`.
Refs are nodes of kind "ref" annotating a character span of their parent leaf.
Term uses are USES_TERM edges, not nodes. See handover/SPEC.md section 2 for the
prose contract and DESIGN.md for the reasoning. Every stage validates its output
against these models before reporting done.
"""
from __future__ import annotations

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


class BBox(BaseModel):
    page: int                      # 1-based absolute PDF page
    bbox: tuple[float, float, float, float]   # x0, y0, x1, y1, PyMuPDF points, origin top-left


class Candidate(BaseModel):
    path: str
    score: float
    reason: Optional[str] = None


class Node(BaseModel):
    """One schema for everything that is ink on the page.

    The branch-or-leaf rule: a node has anatomy children or it has text, never
    both, at any depth. Intro children carry a container's lead-in words. Ref
    children are annotations of a leaf's span, not anatomy, so a leaf may hold
    text and ref children at once.
    """
    id: str                        # sha1(document + version + path), this version's instance
    lineage_key: str               # sha1(document + path), stable across versions
    content_hash: Optional[str] = None   # sha1 of normalised own text, text-bearing nodes only
    path: str                      # core-terms/3/3.1/3.1.2/a ; refs append a span suffix
    kind: Kind
    unit_label: Optional[str] = None     # Clause | Paragraph ... from interpretation clause or profile
    unit_label_source: Optional[Literal["document", "profile"]] = None
    citable: bool = True           # False for intro and ref nodes
    label: Optional[str] = None    # "3.1.2" or "(a)"; queryable lookup key, never identity
    title: Optional[str] = None    # headings only
    text: Optional[str] = None     # own words only; refs hold the pointing words only
    page_start: int
    page_end: int
    printed_page: Optional[str] = None   # from the part's own footer, restarts per part
    bboxes_own: list[BBox] = Field(default_factory=list)
    bboxes_extent: list[BBox] = Field(default_factory=list)
    order: int
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

    # -- table / cell kinds only ----------------------------------------------
    n_rows: Optional[int] = None
    n_cols: Optional[int] = None
    row: Optional[int] = None
    col: Optional[int] = None
    cell_role: Optional[Literal["label", "value", "header"]] = None
    role_confidence: Optional[float] = None         # how plausible the physical role is

    # -- ref kind only ---------------------------------------------------------
    char_span: Optional[tuple[int, int]] = None     # offsets into the PARENT leaf's text
    group_id: Optional[str] = None                  # shared by refs split from one list phrase
    ref_kind: Optional[RefKind] = None
    scope_rule: Optional[ScopeRule] = None
    status: Optional[Status] = None
    target_path: Optional[str] = None               # never minted: unresolved refs keep None
    candidates: list[Candidate] = Field(default_factory=list)
    confidence: Optional[float] = None              # calibrated post-hoc; see EVALUATION.md layer 5
    resolver: Optional[Resolver] = None

    @model_validator(mode="after")
    def _kind_rules(self) -> "Node":
        anatomy_children = [c for c in self.children if c.kind != "ref"]
        ref_children = [c for c in self.children if c.kind == "ref"]

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
            if self.target_path and self.status in ("unresolved",):
                raise ValueError(f"unresolved ref {self.path} carries a target_path")
        else:
            if self.char_span is not None or self.ref_kind is not None:
                raise ValueError(f"{self.kind} node {self.path} carries ref-only fields")
            # branch-or-leaf: anatomy children XOR text
            if anatomy_children and self.text is not None:
                raise ValueError(f"{self.kind} node {self.path} has both text and anatomy children")
            if self.kind == "intro":
                if self.citable:
                    raise ValueError(f"intro node {self.path} must be citable=False")
                if self.children:
                    raise ValueError(f"intro node {self.path} cannot have children")
                if not self.text:
                    raise ValueError(f"intro node {self.path} must carry text")
            if self.kind in ("document", "part", "form_row", "table") and self.text is not None:
                raise ValueError(f"{self.kind} node {self.path} must not carry text")
            if ref_children and self.text is None:
                raise ValueError(f"{self.kind} node {self.path} has ref children but no text to anchor them")
        return self


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
    char_span: tuple[int, int]
    status: Literal["confident", "ambiguous"]
    ambiguity_kind: AmbiguityKind = "none"
    method: Literal["exact_longest", "llm", "human"]
    definition_used: Optional[str] = None   # which DefinitionSite governs at this use


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
