"""Loads stage output from wherever chat.config.DATA_SOURCE points.

Shared substrate for both UIs: the chat tools' fixtures backend reads it, and
the review queue reads it. Everything is validated against pipeline/schemas.py
on the way in, so neither UI ever renders a shape the pipeline could not have
produced.

Note on placement. This module and chat/crops.py serve review-ui/ as well as
chat/. `review-ui` contains a hyphen so it is not an importable package name,
and the ownership map in SPEC section 1 gives ui-builder no third directory to
put shared code in, so the shared substrate lives in the one of the two that
is a legal package. review-ui/review_data.py is the single import site, so
relocating this is a one-line change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.schemas import Concept, DefinitionSite, Node, RefsFile, TermUse

from . import config as ui_config


def _walk(node: Node):
    """Preorder, anatomy and ref children alike."""
    yield node
    for child in node.children:
        yield from _walk(child)


def parent_path_of_ref(ref_path: str) -> str:
    """A ref's path is its parent's path plus /ref@<start>-<end>."""
    marker = "/ref@"
    i = ref_path.rfind(marker)
    if i == -1:
        raise ValueError(f"not a ref path: {ref_path!r}")
    return ref_path[:i]


def part_of(path: str) -> str:
    return path.split("/", 1)[0]


@dataclass
class Corpus:
    """Everything the two UIs read, indexed for lookup."""

    root: Path
    trees: dict[str, Node] = field(default_factory=dict)
    by_path: dict[str, Node] = field(default_factory=dict)
    by_id: dict[str, Node] = field(default_factory=dict)
    refs: list[Node] = field(default_factory=list)
    definition_sites: list[DefinitionSite] = field(default_factory=list)
    term_uses: list[TermUse] = field(default_factory=list)
    concepts: list[Concept] = field(default_factory=list)

    # -- derived indexes -----------------------------------------------------
    refs_by_parent: dict[str, list[Node]] = field(default_factory=dict)
    refs_by_target: dict[str, list[Node]] = field(default_factory=dict)
    uses_by_node: dict[str, list[TermUse]] = field(default_factory=dict)
    sites_by_term: dict[str, list[DefinitionSite]] = field(default_factory=dict)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, root: Path | None = None) -> "Corpus":
        root = root or ui_config.data_root()
        c = cls(root=root)

        for f in sorted((root / "tree").glob("*.json")):
            tree = Node.model_validate(json.loads(f.read_text()))
            c.trees[tree.path] = tree
            for n in _walk(tree):
                c.by_path[n.path] = n
                c.by_id[n.id] = n

        refs_dir = root / "refs"
        if refs_dir.is_dir():
            for f in sorted(refs_dir.glob("*.json")):
                rf = RefsFile.model_validate(json.loads(f.read_text()))
                for ref in rf.refs:
                    c.refs.append(ref)
                    c.by_path[ref.path] = ref
                    c.by_id[ref.id] = ref

        vocab = root / "vocab"
        sites_f = vocab / "definition_sites.json"
        if sites_f.exists():
            c.definition_sites = [
                DefinitionSite.model_validate(d) for d in json.loads(sites_f.read_text())
            ]
        uses_f = vocab / "term_uses.json"
        if uses_f.exists():
            c.term_uses = [TermUse.model_validate(d) for d in json.loads(uses_f.read_text())]

        concepts_f = root / "concepts.json"
        if concepts_f.exists():
            c.concepts = [Concept.model_validate(d) for d in json.loads(concepts_f.read_text())]

        c._index()
        return c

    def _index(self) -> None:
        for ref in self.refs:
            self.refs_by_parent.setdefault(parent_path_of_ref(ref.path), []).append(ref)
            if ref.target_path:
                self.refs_by_target.setdefault(ref.target_path, []).append(ref)
        for use in self.term_uses:
            self.uses_by_node.setdefault(use.node_id, []).append(use)
        for site in self.definition_sites:
            self.sites_by_term.setdefault(site.term, []).append(site)

    # ------------------------------------------------------------- accessors
    def node(self, path: str) -> Node | None:
        return self.by_path.get(path)

    def derived_text(self, path: str) -> str:
        """SPEC 2.1: the full text of a subtree is a derived view produced by
        walking it in `order`, never stored. Ref children annotate a span of
        their parent, they do not contribute text of their own."""
        node = self.by_path.get(path)
        if node is None:
            return ""
        pieces = [
            n.text
            for n in sorted(
                (n for n in _walk(node) if n.kind != "ref" and n.text),
                key=lambda n: n.order,
            )
        ]
        return "\n".join(pieces)

    def anchor_text(self, node: Node) -> str:
        """The text a char_span indexes into. Heading matches offset into the
        title, per SPEC 2.3; everything else into `text`."""
        return node.text if node.text is not None else (node.title or "")

    def ancestors(self, path: str) -> list[Node]:
        out, segs = [], path.split("/")
        for i in range(1, len(segs)):
            anc = self.by_path.get("/".join(segs[:i]))
            if anc is not None:
                out.append(anc)
        return out

    def first_box(self, node: Node) -> dict | None:
        boxes = node.bboxes_own or node.bboxes_extent
        if not boxes:
            return None
        b = boxes[0]
        return {"page": b.page, "bbox": list(b.bbox)}

    def governing_site(self, term: str, part: str | None) -> DefinitionSite | None:
        """Part-local definitions shadow document-level ones inside their part
        (SPEC 2.3). Resolution order is part-local first, then document."""
        sites = self.sites_by_term.get(term, [])
        if part:
            for s in sites:
                if s.scope == f"part:{part}":
                    return s
        for s in sites:
            if s.scope == "document":
                return s
        return sites[0] if sites else None


_CACHE: dict[str, Corpus] = {}


def corpus(reload: bool = False) -> Corpus:
    key = str(ui_config.data_root())
    if reload or key not in _CACHE:
        _CACHE[key] = Corpus.load()
    return _CACHE[key]
