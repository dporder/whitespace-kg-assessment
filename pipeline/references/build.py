"""Turning a resolved pointer into a `ref` Node, with ids that match the tree.

A ref's id is `sha1("{document}|{version}|{path}")` like every other node's,
so stage 3 has to mint ids under the same document and version stage 2 used.
A part tree carries neither string outright, so this module derives both from
the ids the tree already holds: `lineage_key` is a hash of document and path
alone, so the document falls out of a one-variable search, and each part's
version then falls out of a one-variable search against that part's `id`.

The version is per part. Stage 2 builds its Context with
`version=part["template_version"]`, and this pack binds about forty-eight
separately versioned templates, so Core Terms mints ids under v3.0.11 while the
Award Form uses v3.10. A ref minted under the wrong one would never join its
parent in the graph. If a search does not land, that is reported as a violation
and the run exits 2 rather than writing refs whose ids can never join up.
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
    """The document id, and the version each part's ids were minted under.

    The version is per part, not per document, because that is what stage 2
    actually does: `pipeline/assemble/tree.py` builds its Context with
    `version=part["template_version"]`, and this pack is a binding of about
    forty-eight separately versioned templates, so Core Terms mints ids under
    v3.0.11 and the Award Form under v3.10. A ref's id has to be minted under
    the same version as the provision it hangs off, or it will never join up
    with its parent in the graph.
    """
    document: str
    versions: dict[str, str]
    verified: bool
    note: str
    default_version: str = "v1"

    def version_for(self, path: str) -> str:
        return self.versions.get(path.split("/", 1)[0], self.default_version)

    @property
    def version(self) -> Optional[str]:
        """The one version, when every part agrees. None when they do not."""
        distinct = set(self.versions.values())
        return distinct.pop() if len(distinct) == 1 else None

    def as_dict(self) -> dict:
        return {"document": self.document, "versions": dict(sorted(self.versions.items())),
                "one_version_for_every_part": self.version, "verified": self.verified,
                "note": self.note}


def infer_identity(roots: Iterable[Node], *, document: Optional[str] = None,
                   version: Optional[str] = None) -> Identity:
    """Derive the document id, then each part's own version, from the trees' ids.

    `lineage_key` is a hash of document and path alone, so the document falls
    out of a one-variable search. Each part's version then falls out of a
    one-variable search against that part's `id`.
    """
    roots = list(roots)
    if not roots:
        return Identity(document or config.DOCUMENT_ID, {}, False,
                        "no trees to derive identity from", version or "v1")
    docs = ([document] if document else []) + list(DOCUMENT_CANDIDATES)
    docs += [str(r.source_file).rsplit(".", 1)[0] for r in roots if r.source_file]
    found_doc = next((d for d in docs
                      if all(lineage_key(d, r.path) == r.lineage_key for r in roots)), None)
    if found_doc is None:
        return Identity(document or config.DOCUMENT_ID, {}, False,
                        f"no candidate document id reproduces the trees' lineage keys; "
                        f"tried {docs}", version or "v1")

    versions: dict[str, str] = {}
    unmatched: list[str] = []
    for root in roots:
        candidates = ([version] if version else []) + [
            root.template_version, root.version_label, *VERSION_CANDIDATES]
        found = next((v for v in candidates
                      if v and node_id(found_doc, v, root.path) == root.id), None)
        if found is None:
            unmatched.append(root.path)
        else:
            versions[root.path] = found
    if unmatched:
        return Identity(found_doc, versions, False,
                        f"document id {found_doc!r} verified from lineage keys, but no "
                        f"candidate version reproduces the node id of {unmatched}",
                        version or "v1")
    return Identity(found_doc, versions, True,
                    f"document and per-part versions all reproduce the trees' own ids "
                    f"({len(versions)} part(s))", version or "v1")


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
        # The version is the citing part's, because that is the version its
        # parent's id was minted under (stage 2 keys on template_version).
        id=node_id(identity.document, identity.version_for(path), path),
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
