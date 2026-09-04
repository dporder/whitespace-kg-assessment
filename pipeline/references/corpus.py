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

# An Annex or a Part is named in a heading's title, not numbered in its label:
# Call-Off Schedule 9 prints "Part B - Annex 1: Baseline security requirements".
# Indexing those titles is what lets "Annex 1" find the annex instead of
# falling through to a paragraph that merely happens to be numbered 1.
_DIVISION = re.compile(r"\b(Annex|Appendix|Part|Table)\s+([A-Z0-9]+)\b", re.I)


def walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from walk(child)


def normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


# A division the drafters themselves named as a Part. Call-Off Schedule 9
# prints "Part B: Long Form Security Requirements" as a heading and restarts its
# paragraph numbering underneath it, so "Paragraph 3.4" cited inside Part B
# means Part B's, not the same-numbered paragraph in Part A. SPEC 2.2 pins that
# as the nearest enclosing scope for resolution, mirroring definition scoping in
# 2.3. The pattern is imported from the vocabulary side rather than copied, so
# there is one definition of what a drafter-named Part looks like and the two
# tiers cannot drift apart; if that module ever stops being importable the
# mirrored fallback keeps this working and says so in the run report.
try:                                                      # pragma: no cover - trivial
    from pipeline.vocabulary.declared import NAMED_SUB_PART
    NAMED_SUB_PART_SOURCE = "pipeline.vocabulary.declared.NAMED_SUB_PART"
except Exception:                                         # noqa: BLE001
    NAMED_SUB_PART = re.compile(r"^\s*Part\s+[A-Z0-9]+\b")
    NAMED_SUB_PART_SOURCE = "mirrored locally: the vocabulary module did not import"


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
    # Numbering restarts inside a drafter-named Part, so each one is its own
    # namespace, and so is what is left outside them all.
    sub_parts: list[str] = field(default_factory=list)
    sub_part_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    outside_labels: dict[str, str] = field(default_factory=dict)
    # "annex 1" -> path, from the titles the drafters wrote, per namespace.
    divisions: dict[str, str] = field(default_factory=dict)
    sub_part_divisions: dict[str, dict[str, str]] = field(default_factory=dict)
    outside_divisions: dict[str, str] = field(default_factory=dict)

    def sub_part_for(self, node_path: str) -> Optional[str]:
        """The nearest enclosing drafter-named Part, or None if outside them all."""
        best = None
        for candidate in self.sub_parts:
            if node_path == candidate or node_path.startswith(candidate + "/"):
                if best is None or len(candidate) > len(best):
                    best = candidate
        return best

    def namespace_for(self, node_path: Optional[str]) -> dict[str, str]:
        """The label namespace a citation standing at `node_path` reads in."""
        if node_path is None:
            return self.labels
        sub = self.sub_part_for(node_path)
        return self.sub_part_labels.get(sub, {}) if sub else self.outside_labels

    def division_namespace(self, node_path: Optional[str]) -> dict[str, str]:
        """The same, for divisions named in a title rather than numbered."""
        if node_path is None:
            return self.divisions
        sub = self.sub_part_for(node_path)
        return self.sub_part_divisions.get(sub, {}) if sub else self.outside_divisions

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
            # Named divisions first: every later label is filed under the one it
            # sits in, because that is the namespace its number belongs to.
            info.sub_parts = sorted(
                node.path for node in walk(root)
                if node.kind != "ref" and NAMED_SUB_PART.match(node.title or ""))
            for path in info.sub_parts:
                info.sub_part_labels[path] = {}
                info.sub_part_divisions[path] = {}
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
                        home = info.sub_part_for(node.path)
                        space = (info.sub_part_labels[home] if home
                                 else info.outside_labels)
                        space.setdefault(bare, node.path)
                        space.setdefault(label, node.path)
                if node.title:
                    info.titles.setdefault(normalise_title(node.title), node.path)
                    for unit, number in _DIVISION.findall(node.title):
                        key = f"{unit.lower()} {number.lower()}"
                        info.divisions.setdefault(key, node.path)
                        # A division names itself, so it belongs to the namespace
                        # it sits IN, not the one it opens.
                        outer = info.sub_part_for(node.path.rsplit("/", 1)[0]) \
                            if "/" in node.path else None
                        space = (info.sub_part_divisions[outer] if outer
                                 else info.outside_divisions)
                        space.setdefault(key, node.path)
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

    def lookup(self, part_id: str, number: str, item: Optional[str] = None,
               within: Optional[str] = None) -> Optional[str]:
        """The path of `number` inside `part_id`, or None. Never invents one.

        `within` is the path of the citing provision, and it decides which
        namespace the number is read in. Call-Off Schedule 9's Part B restarts
        paragraph numbering, so "Paragraph 3.4" cited inside Part B means Part
        B's 3.4 and not Part A's, which is what SPEC 2.2 pins as the nearest
        enclosing scope. A number that exists only outside the citing division
        still falls through to the part, because a citation reaching across a
        Part boundary is ordinary; one that could have stayed home is not.
        """
        info = self.parts.get(part_id)
        if info is None:
            return None
        base = None
        if within is not None:
            base = info.namespace_for(within).get(number)
        if base is None:
            base = info.labels.get(number)
        if base is None:
            return None
        if not item:
            return base
        return info.items.get((base, item.lower()))

    def lookup_division(self, part_id: str, unit: str, number: str,
                        within: Optional[str] = None) -> Optional[str]:
        """An Annex or Part by the name its heading carries, or None.

        Never falls through to a bare number. "Annex 2" cited in Call-Off
        Schedule 9 used to land on the paragraph numbered 2, because the label
        forms ended in a bare "2" and a paragraph carries that label. An Annex
        that does not exist must stay unresolved, not resolve to something else
        of the same number.
        """
        info = self.parts.get(part_id)
        if info is None:
            return None
        key = f"{unit.strip().rstrip('s').lower()} {number.lower()}"
        if within is not None:
            found = info.division_namespace(within).get(key)
            if found:
                return found
        return info.divisions.get(key)

    def crossed_sub_part(self, part_id: str, target_path: str,
                         within: Optional[str]) -> Optional[tuple[str, str]]:
        """(citing division, target division) when a resolution left its Part.

        Reported, not prevented: a citation really can point across a Part
        boundary. It is the ones that could have stayed home that were the bug,
        and those no longer happen.
        """
        info = self.parts.get(part_id)
        if info is None or within is None:
            return None
        here, there = info.sub_part_for(within), info.sub_part_for(target_path)
        return None if here == there else (here or part_id, there or part_id)

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
