"""Frozen data contracts. Owned by the orchestrator. Workers import, never edit.

Every stage validates its output against these before reporting done.
See handover/SPEC.md for the prose contract and the reasoning behind each field.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field

Status = Literal["resolved", "ambiguous", "unresolved", "external"]
Kind = Literal["part", "heading", "clause", "subclause", "item", "intro",
               "form_row", "table", "cell", "preamble"]


class BBox(BaseModel):
    page: int                      # 1-based absolute PDF page
    bbox: tuple[float, float, float, float]   # x0, y0, x1, y1, PyMuPDF points, origin top-left


class DocNode(BaseModel):
    """A node in the provision tree. Has children or has text, never both."""
    id: str                        # sha1(document + version + path), this version's instance
    lineage_key: str               # sha1(document + path), stable across versions
    content_hash: Optional[str] = None   # sha1 of normalised own text, leaves only
    path: str                      # core-terms/3/3.1/3.1.2/a
    kind: Kind
    unit_label: Optional[str] = None     # Clause | Paragraph, from interpretation clause or profile
    unit_label_source: Optional[Literal["document", "profile"]] = None
    citable: bool = True           # False for intro nodes, which have no legal identity
    label: Optional[str] = None    # "3.1.2" or "(a)"
    title: Optional[str] = None    # headings only
    text: Optional[str] = None     # own words only, leaves only, null on containers
    page_start: int
    page_end: int
    printed_page: Optional[str] = None   # from the part's own footer, restarts per part
    bboxes_own: list[BBox] = Field(default_factory=list)
    bboxes_extent: list[BBox] = Field(default_factory=list)
    order: int
    children: list["DocNode"] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    path: str
    score: float
    reason: Optional[str] = None


class Reference(BaseModel):
    id: str
    source_node_id: str
    raw: str
    char_span: tuple[int, int]
    bbox: Optional[BBox] = None    # stored at parse time so review can highlight without reparsing
    kind: Literal["clause", "schedule", "paragraph", "annex", "part",
                  "definition", "legislation", "unknown"]
    scope_rule: Literal["js1_1.3.8", "js1_1.3.9", "title_paren", "same_part", "none"]
    status: Status
    target_path: Optional[str] = None
    candidates: list[Candidate] = Field(default_factory=list)
    confidence: float = 0.0
    resolver: Literal["regex", "scope", "llm", "human"]
    expansion: list[str] = Field(default_factory=list)
    note: Optional[str] = None     # why ambiguous, e.g. mislabelled Clause inside a Schedule


class TermDefinition(BaseModel):
    term: str
    definition_node_id: str
    source: Literal["js1", "inline"]
    scope_part: Optional[str] = None     # null means document-wide, else local to this part
    pointer: Optional[str] = None        # when the definition delegates elsewhere
    discovered_by: Literal["rule", "given_list", "both"]


class TermUse(BaseModel):
    term: str
    node_id: str
    char_span: tuple[int, int]
    status: Literal["confident", "ambiguous"]
    method: Literal["exact_longest", "llm", "human"]
    position: Literal["body", "heading", "sentence_initial"]
    resolved_definition_id: Optional[str] = None   # local shadows document-wide
    audit_sampled: bool = False


class Concept(BaseModel):
    id: str
    label: str
    scope_path: str
    member_node_ids: list[str] = Field(default_factory=list)
    relations: list[dict] = Field(default_factory=list)
    llm_derived: bool = True
    confidence: float = 0.0


class EmbeddingRecord(BaseModel):
    node_id: str
    level: Literal["leaf_text", "subtree_text", "summary"]
    text: str
    vector_ref: str
    llm_derived: bool = False      # True for summary level


class ProfileFit(BaseModel):
    profile: str
    fits: bool
    signals: dict                  # which of the five fired, with examples
    quarantined: bool = False


DocNode.model_rebuild()
