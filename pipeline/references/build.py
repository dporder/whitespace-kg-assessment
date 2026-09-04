"""Turning a resolved pointer into a `ref` Node, with ids that match the tree.

A ref's id is `sha1("{document}|{version}|{path}")` like every other node's,
so stage 3 has to mint ids under the same document and version stage 2 used.
A part tree does not carry either string: `version_label` is a document-root
field and the tree files are per part. Rather than guess, this module derives
them from the ids the tree already holds. `lineage_key` is a hash of document
and path alone, so the document falls out of a one-variable search; the version
then falls out of a one-variable search against `id`. If neither search lands,
that is reported as a violation and the run exits 2 rather than writing refs
whose ids will never join up with their parents in the graph.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import config
from pipeline.schemas import BBox, Candidate, Node, lineage_key, node_id

from .detect import Pointer
from .resolve import Resolution

# Tried in order against the ids the trees already carry.
DOCUMENT_CANDIDATES = (config.DOCUMENT_ID, f"{config.DOCUMENT_ID}-fixture")
VERSION_CANDIDATES = ("v1", "v3.0.11", "dev", "v0", config.DOCUMENT_ID)


@dataclass
class Identity:
    document: str
    version: str
    verified: bool
    note: str

    def as_dict(self) -> dict:
        return {"document": self.document, "version": self.version,
                "verified": self.verified, "note": self.note}


def infer_identity(roots: Iterable[Node], *, document: Optional[str] = None,
                   version: Optional[str] = None) -> Identity:
    """Derive the document and version the trees' own ids were minted under."""
    roots = list(roots)
    if not roots:
        return Identity(document or config.DOCUMENT_ID, version or "v1", False,
                        "no trees to derive identity from")
    docs = ([document] if document else []) + [d for d in DOCUMENT_CANDIDATES]
    extra = [r.source_file for r in roots if r.source_file]
    docs += [str(s).rsplit(".", 1)[0] for s in extra]
    found_doc = next((d for d in docs
                      if all(lineage_key(d, r.path) == r.lineage_key for r in roots)), None)
    if found_doc is None:
        return Identity(document or config.DOCUMENT_ID, version or "v1", False,
                        f"no candidate document id reproduces the trees' lineage keys; "
                        f"tried {docs}")
    versions = ([version] if version else []) + list(VERSION_CANDIDATES)
    versions += [r.version_label for r in roots if r.version_label]
    versions += [r.template_version for r in roots if r.template_version]
    found_version = next((v for v in versions if v and
                          all(node_id(found_doc, v, r.path) == r.id for r in roots)), None)
    if found_version is None:
        return Identity(found_doc, version or "v1", False,
                        f"document id {found_doc!r} verified from lineage keys, but no "
                        f"candidate version reproduces the trees' node ids; tried "
                        f"{[v for v in versions if v]}")
    return Identity(found_doc, found_version, True,
                    "document and version both reproduce the trees' own ids")


def ref_node(pointer: Pointer, resolution: Resolution, parent: Node,
             identity: Identity, *, order: int, batch_id: Optional[str]) -> Node:
    """One ref node: the pointing words, where they sit, and where they point."""
    path = pointer.path
    anomalies: list[str] = []
    if pointer.method == "llm":
        anomalies.append("detected_by_llm_span_extraction: the citation grammar and the "
                         "orphan scan did not cover these characters")
    if parent.page_start != parent.page_end:
        anomalies.append(f"ref_page_inferred_from_parent_start: the citing node spans "
                         f"pages {parent.page_start} to {parent.page_end}")
    anomalies.extend(resolution.notes)
    anomalies.extend(pointer.notes)

    return Node(
        id=node_id(identity.document, identity.version, path),
        lineage_key=lineage_key(identity.document, path),
        path=path,
        kind="ref",
        citable=False,
        text=pointer.text,
        page_start=parent.page_start,
        page_end=parent.page_start,
        printed_page=parent.printed_page,
        # Stage 3 reads trees, not layout, so a tight box for the citing
        # characters cannot be computed here. The citing node's own boxes do
        # contain those characters, so they are carried as the widest true box
        # rather than a narrower invented one. See the run report.
        bboxes_own=[BBox(page=b.page, bbox=b.bbox) for b in parent.bboxes_own],
        order=order,
        batch_id=batch_id or parent.batch_id,
        anomalies=anomalies,
        char_span=(pointer.span[0], pointer.span[1]),
        group_id=pointer.group_id,
        ref_kind=pointer.ref_kind,
        scope_rule=resolution.scope_rule,
        status=resolution.status,
        target_path=resolution.target_path,
        candidates=[Candidate(path=c.path, score=c.score, reason=c.reason)
                    for c in resolution.candidates],
        confidence=resolution.confidence,
        resolver=resolution.resolver,
    )


def span_intact(ref: Node, parent: Node) -> bool:
    """The ref's own words must be exactly the characters it claims."""
    text = parent.text or ""
    start, end = ref.char_span or (0, 0)
    return 0 <= start <= end <= len(text) and text[start:end] == (ref.text or "")
