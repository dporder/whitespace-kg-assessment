"""Reading stage 2 trees, and the derived views stages 4 to 6 all need.

Shared by the three enrichment stages (vocabulary, concepts, embeddings), which
are one worker's and are therefore free to import each other's code; what they
must never do is read each other's *output* (SPEC 3, "stages 3 to 6 share the
trees and none reads another's output"). This module reads trees only.

Nothing here alters source text. `subtree_text` concatenates the stored text of
a subtree in reading order to make the derived view SPEC 2.1 describes as
derived and never stored; it is an input to matching, scanning and embedding,
and it is never written back onto a node.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from pipeline.schemas import Node

# The subtree text join. One space keeps character offsets meaningful and does
# not invent paragraph structure the document did not print.
JOIN = " "

# Anatomy kinds that can hold anatomy children. Everything else is a leaf by
# construction (SPEC 2.1's per-kind table).
CONTAINER_KINDS = ("document", "part", "heading", "preamble", "clause",
                   "subclause", "item", "form_row", "table")


def walk(node: Node) -> Iterator[Node]:
    """Preorder, children in stored order. Deterministic."""
    yield node
    for child in node.children:
        yield from walk(child)


def anatomy_children(node: Node) -> list[Node]:
    return [c for c in node.children if c.kind != "ref"]


def is_leaf(node: Node) -> bool:
    """A node with no anatomy children. Ref children do not make a branch."""
    return not anatomy_children(node)


def subtree_text(node: Node) -> str:
    """The derived full text of a subtree, walked in order (SPEC 2.1).

    Titles are included for headings and parts because a heading's title is its
    own ink and a scan unit that drops it loses what the clause is called.
    """
    parts: list[str] = []
    for n in walk(node):
        if n.kind == "ref":
            continue
        if n.title:
            parts.append(n.title)
        if n.text:
            parts.append(n.text)
    return JOIN.join(p for p in parts if p)


def own_texts(node: Node) -> list[tuple[str, str]]:
    """(field, value) for the node's own matchable text: its text and title.

    `char_span` offsets are into `text`, or into `title` for heading matches
    (SPEC 2.3), so the two fields are kept apart rather than concatenated.
    """
    out: list[tuple[str, str]] = []
    if node.text:
        out.append(("text", node.text))
    if node.title:
        out.append(("title", node.title))
    return out


def part_id(part: Node) -> str:
    """A part's id is the first segment of its path."""
    return part.path.split("/", 1)[0]


@dataclass(frozen=True)
class Section:
    """A scan unit: a part's top-level child, or the part itself when it has
    none. This is the unit the typo-density signal is computed over (SPEC 2.3,
    "a deterministic per section typo density signal") and one of the two units
    the concept scan uses (SPEC 2.4, "a part or top level clause").
    """
    part: str
    node: Node

    @property
    def path(self) -> str:
        return self.node.path


def sections(part: Node) -> list[Section]:
    """Top-level children of a part; the part itself when it has none."""
    pid = part_id(part)
    children = anatomy_children(part)
    if not children:
        return [Section(part=pid, node=part)]
    return [Section(part=pid, node=c) for c in children]


def section_of(part: Node) -> dict[str, str]:
    """node id -> the path of the section that contains it."""
    out: dict[str, str] = {}
    for sec in sections(part):
        for n in walk(sec.node):
            out[n.id] = sec.path
    out.setdefault(part.id, part.path)
    return out


# ---------------------------------------------------------------- loading


@dataclass
class Trees:
    """Loaded stage 2 trees plus the record of where they came from."""
    source: str                      # "output" or "fixtures"
    root: Path
    run: str
    parts: dict[str, Node]           # part id -> part node, insertion-sorted
    files: dict[str, Path]

    def ordered(self) -> list[tuple[str, Node]]:
        return [(p, self.parts[p]) for p in sorted(self.parts)]

    def nodes(self) -> Iterator[tuple[str, Node]]:
        for p, node in self.ordered():
            yield from ((p, n) for n in walk(node))

    def by_id(self) -> dict[str, Node]:
        return {n.id: n for _p, n in self.nodes()}

    def by_path(self) -> dict[str, Node]:
        return {n.path: n for _p, n in self.nodes()}


def discover_parts(source_root: Path) -> list[str]:
    tree_dir = source_root / "tree"
    if not tree_dir.is_dir():
        return []
    return sorted(p.stem for p in tree_dir.glob("*.json"))


def has_trees(run_dir: Path) -> bool:
    d = run_dir / "tree"
    return d.is_dir() and any(d.glob("*.json"))


def newest_run(output_root: Path) -> Optional[str]:
    if not output_root.is_dir():
        return None
    runs = [d for d in output_root.iterdir() if d.is_dir() and has_trees(d)]
    if not runs:
        return None
    return sorted(runs, key=lambda d: (d.stat().st_mtime, d.name))[-1].name


def load_trees(source: str, source_root: Path, run: str,
               parts: Optional[list[str]] = None) -> Trees:
    """Load every stage 2 tree in scope. Raises on a tree that will not
    validate: an enrichment stage standing on a broken tree would produce
    confident nonsense, which is the failure the whole build exists to avoid."""
    wanted = parts if parts is not None else discover_parts(source_root)
    loaded: dict[str, Node] = {}
    files: dict[str, Path] = {}
    for part in wanted:
        path = source_root / "tree" / f"{part}.json"
        if not path.exists():
            raise FileNotFoundError(f"no stage 2 tree for part {part!r} at {path}")
        loaded[part] = Node.model_validate(json.loads(path.read_text()))
        files[part] = path
    return Trees(source=source, root=source_root, run=run, parts=loaded, files=files)
