"""What the corpus actually contains, which is what resolution is allowed to hit.

Refs never mint target nodes (SPEC 2.2), so every resolution is a lookup in
this index and nothing else. A citation to a part that has not been ingested
finds nothing here, and the resolver keeps it unresolved with the conventional
path as a candidate string, never as a node.

Two lookups matter. By part and printed number, because numbering restarts in
every part and "3.1.2" is only unique inside one. And by part title, because
"Schedule 6 (ICT Services)" disambiguates a schedule family by its title.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

from pipeline.schemas import Node

# The pack's part-id convention, used to mint candidate paths for parts that
# have not arrived yet. A candidate is a string; it never becomes a node.
SCHEDULE_FAMILIES = ("framework-schedule", "joint-schedule", "call-off-schedule")
FAMILY_BY_WORD = {"framework": "framework-schedule", "joint": "joint-schedule",
                  "call-off": "call-off-schedule", "call off": "call-off-schedule",
                  "calloff": "call-off-schedule"}
CORE_PART = "core-terms"

_ITEM = re.compile(r"^\(?([a-z]{1,3}|[ivxlcdm]+)\)?$", re.I)


def walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from walk(child)


def normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


@dataclass
class PartInfo:
    id: str
    title: Optional[str]
    family: Optional[str]
    unit_label: Optional[str]
    root: Node
    labels: dict[str, str] = field(default_factory=dict)      # "3.1.2" -> path
    items: dict[tuple[str, str], str] = field(default_factory=dict)  # (parent, "a") -> path
    titles: dict[str, str] = field(default_factory=dict)      # normalised title -> path

    @property
    def is_core(self) -> bool:
        return self.family == "core" or self.id == CORE_PART

    @property
    def is_schedule(self) -> bool:
        return bool(self.family and "schedule" in self.family)


@dataclass
class Corpus:
    """Every ingested tree, indexed for resolution.

    `known_parts` is the optional register of parts the document has but this
    run has not ingested, part id to title, from `output/<run>/parts.json` or
    the profile when stage 0's full structural pass has written one. It is used
    for candidates only, never for targets: a part that is not here cannot be a
    resolved target, and knowing its title is what turns "one of three families"
    into "that one, not yet loaded".
    """
    parts: dict[str, PartInfo] = field(default_factory=dict)
    paths: dict[str, Node] = field(default_factory=dict)
    known_parts: dict[str, Optional[str]] = field(default_factory=dict)

    def known_by_title(self, title: str) -> list[str]:
        """Part ids in the register whose title matches, ingested or not."""
        key = normalise_title(title)
        if not key:
            return []
        out = []
        for part_id, part_title in self.known_parts.items():
            own = normalise_title(part_title or "")
            if own and (own == key or key in own):
                out.append(part_id)
        return sorted(set(out))

    # -- construction --------------------------------------------------------
    @classmethod
    def from_trees(cls, trees: dict[str, Node]) -> "Corpus":
        corpus = cls()
        for part_id, root in trees.items():
            info = PartInfo(id=part_id, title=root.title, family=root.part_family,
                            unit_label=root.unit_label, root=root)
            for node in walk(root):
                corpus.paths[node.path] = node
                if node.kind == "ref":
                    continue
                label = (node.label or "").strip()
                if label:
                    bare = label.strip("()").strip()
                    if _ITEM.match(label) or _ITEM.match(bare):
                        parent = node.path.rsplit("/", 1)[0]
                        info.items.setdefault((parent, bare.lower()), node.path)
                    else:
                        info.labels.setdefault(bare, node.path)
                        info.labels.setdefault(label, node.path)
                if node.title:
                    info.titles.setdefault(normalise_title(node.title), node.path)
            corpus.parts[part_id] = info
            corpus.known_parts.setdefault(part_id, root.title)
        return corpus

    def register_parts(self, registry: dict[str, Optional[str]]) -> None:
        """Add parts the document has but this run has not ingested."""
        for part_id, title in registry.items():
            if part_id in self.parts:
                continue
            if title or part_id not in self.known_parts:
                self.known_parts[part_id] = title

    # -- lookups -------------------------------------------------------------
    def exists(self, path: str) -> bool:
        return path in self.paths

    def node(self, path: str) -> Optional[Node]:
        return self.paths.get(path)

    def part_of(self, path: str) -> str:
        return path.split("/", 1)[0]

    def enclosing_part(self, path: str) -> Optional[PartInfo]:
        """The nearest enclosing part, by walking the path, never by distance."""
        return self.parts.get(self.part_of(path))

    def lookup(self, part_id: str, number: str, item: Optional[str] = None) -> Optional[str]:
        """The path of `number` inside `part_id`, or None. Never invents one."""
        info = self.parts.get(part_id)
        if info is None:
            return None
        base = info.labels.get(number)
        if base is None:
            return None
        if not item:
            return base
        return info.items.get((base, item.lower()))

    def lookup_titled(self, title: str) -> list[str]:
        """Paths whose title matches, exactly on the normalised form."""
        key = normalise_title(title)
        if not key:
            return []
        return sorted({p for info in self.parts.values()
                       for t, p in info.titles.items() if t == key})

    def parts_matching_title(self, title: str) -> list[str]:
        """Part ids whose own title matches, for `Schedule 6 (ICT Services)`."""
        key = normalise_title(title)
        if not key:
            return []
        out = []
        for info in self.parts.values():
            own = normalise_title(info.title or "")
            if not own:
                continue
            if own == key or key in own or own.endswith(key):
                out.append(info.id)
        return sorted(out)

    # -- the naming convention, for targets that have not been ingested -------
    @staticmethod
    def schedule_part_id(family: Optional[str], number: str) -> Optional[str]:
        if not family:
            return None
        stem = FAMILY_BY_WORD.get(family.lower().replace("-", "-"))
        if stem is None:
            stem = FAMILY_BY_WORD.get(re.sub(r"[\s-]+", "-", family.lower()))
        if stem is None:
            return None
        return f"{stem}-{number.replace('.', '-')}"

    @staticmethod
    def schedule_candidates(number: str) -> list[str]:
        """The three parts a bare `Schedule N` could name, by convention."""
        return [f"{stem}-{number.replace('.', '-')}" for stem in SCHEDULE_FAMILIES]
