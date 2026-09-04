"""The geometric invariants from SPEC 2.1, checked and reported, never repaired.

Four checks cross a tree built from numbering against the geometry that built
it, so a mis-parented node the numbering alone would accept still shows up.

1. A child's left edge is at or right of its parent's.
2. A node's own box sits at or above its first child's.
3. Siblings do not overlap vertically on a page and ascend in reading order.
4. A node's extent stays inside its parent's extent.

Every violation is recorded on the node and in `violations.json`. What the
report also does is separate two things a raw violation count conflates.

An *isolated* violation is evidence of a mis-built tree: one clause sitting
where its siblings do not. A violation that fires uniformly across an entire
level of an entire part is evidence of typesetting, not of parsing: Core Terms
indents its 18pt top-level headings to x=30.4 and their 12pt clause children to
x=27.0, so check 1 fires on every one of the 146 clauses and means nothing about
parentage. Those are labelled `systematic_level_offset` with the measurement
that identifies them, and counted separately, which is what "zero unexplained
violations" is measured against. Nothing is hidden either way: both counts and
every instance are in the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.parse.geometry import (
    INDENT_TOLERANCE,
    VERTICAL_TOLERANCE,
    sibling_overlap_tolerance,
)
from pipeline.schemas import Node

CHECKS = (
    "child_left_of_parent",
    "own_box_below_first_child",
    "siblings_overlap_vertically",
    "siblings_out_of_reading_order",
    "extent_escapes_parent",
)

# A violation is systematic when it fires on this share of the comparable pairs
# at the same (part, parent kind, child kind), over at least this many pairs.
# Below either bar it is isolated and stays unexplained.
SYSTEMATIC_SHARE = 0.8
SYSTEMATIC_MIN_COUNT = 3


@dataclass(frozen=True)
class VerifiedRender:
    """One page whose geometry was checked by rendering it and reading the ink.

    A violation the parser cannot explain from the numbers alone is not
    necessarily a mis-parse; sometimes the page really is set that way. The
    only way to tell is to look, so each entry here records a page that was
    rendered and what was seen on it. Entries are added for pages a person has
    actually looked at, never to quieten a count: an unverified violation stays
    unexplained, which is what keeps the number meaningful.
    """
    part: str
    page: int
    check: str
    seen: str


# Pages rendered at 2x and read during the parser build. Every one of these
# shows the document printing a child further left than the parent above it,
# which is this pack's house style for hanging-indented sub-paragraphs, not a
# mis-parented node.
VERIFIED_TYPESETTING: tuple[VerifiedRender, ...] = (
    VerifiedRender(
        part="call-off-schedule-9", page=344, check="child_left_of_parent",
        seen="paragraph 5.2.1 is printed at the left margin, left of its parent 5.2",
    ),
    VerifiedRender(
        part="call-off-schedule-9", page=347, check="child_left_of_parent",
        seen="paragraphs 2.3.1 and 2.3.2 are printed left of their parent 2.3",
    ),
    VerifiedRender(
        part="framework-schedule-7", page=95, check="child_left_of_parent",
        seen="the lettered items under 3.1.4 hang left of the number above them",
    ),
    VerifiedRender(
        part="framework-schedule-7", page=97, check="child_left_of_parent",
        seen="every lettered item on the page hangs left of its parent's number",
    ),
)


@dataclass
class Violation:
    check: str
    path: str
    parent_path: Optional[str]
    detail: str
    measure: float
    kind: str
    parent_kind: Optional[str]
    explained: Optional[str] = None
    page: Optional[int] = None

    def as_json(self) -> dict:
        return {
            "check": self.check,
            "path": self.path,
            "parent_path": self.parent_path,
            "kind": self.kind,
            "parent_kind": self.parent_kind,
            "page": self.page,
            "detail": self.detail,
            "measure": round(self.measure, 2),
            "explained": self.explained,
        }


@dataclass
class InvariantReport:
    part: str
    violations: list[Violation] = field(default_factory=list)
    # (check, parent kind, child kind) -> how many comparable pairs were tested.
    # The denominator of the systematic test, so "all 146 of them" can be told
    # apart from "3 of 146".
    tested: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def record_test(self, check: str, parent_kind: str, child_kind: str) -> None:
        key = (check, parent_kind, child_kind)
        self.tested[key] = self.tested.get(key, 0) + 1

    @property
    def unexplained(self) -> list[Violation]:
        return [v for v in self.violations if v.explained is None]

    def counts(self) -> dict[str, int]:
        out = {c: 0 for c in CHECKS}
        for v in self.violations:
            out[v.check] += 1
        return out

    def as_json(self) -> dict:
        return {
            "part": self.part,
            "total": len(self.violations),
            "unexplained": len(self.unexplained),
            "by_check": self.counts(),
            "violations": [v.as_json() for v in self.violations],
        }


def _own_boxes(node: Node) -> list:
    return list(node.bboxes_own)


def _page_box(boxes: Iterable, page: int):
    for b in boxes:
        if b.page == page:
            return b.bbox
    return None


def check_tree(part_id: str, root: Node) -> InvariantReport:
    report = InvariantReport(part=part_id)
    _walk(root, None, report)
    _explain(report)
    return report


def _walk(node: Node, parent: Optional[Node], report: InvariantReport) -> None:
    children = [c for c in node.children if c.kind != "ref"]

    if parent is not None:
        _check_left(node, parent, report)
        _check_extent(node, parent, report)
    if children:
        _check_own_above_first_child(node, children[0], report)
    for i in range(len(children) - 1):
        _check_sibling_order(children[i], children[i + 1], report)
    for child in children:
        _walk(child, node, report)


def _check_left(node: Node, parent: Node, report: InvariantReport) -> None:
    if parent.kind in ("document", "part"):
        # A part is the root of the page, not an indent level. Its own ink is
        # its cover title, and comparing a form row's indent against a cover
        # title's says nothing about parentage.
        return
    if parent.kind in ("table", "form_row"):
        # Cells partition their row horizontally, so a cell to the right of
        # another is not nested under it. Their geometry is checked by the
        # row-major sibling rule instead.
        return
    own = _own_boxes(node) or node.bboxes_extent
    parent_own = _own_boxes(parent) or parent.bboxes_extent
    if not own or not parent_own:
        return
    # Indentation only means anything within one page. A node that wraps over a
    # page break resumes at the next page's left margin, which is further left
    # than its own number was, and comparing that against a parent whose number
    # sits on the previous page compares two different pages' margins. Where
    # parent and child share a page, that page decides; otherwise the child's
    # first page is compared against the parent's nearest box.
    shared = sorted({b.page for b in own} & {b.page for b in parent_own})
    if shared:
        page = shared[0]
        left = min(b.bbox[0] for b in own if b.page == page)
        parent_left = min(b.bbox[0] for b in parent_own if b.page == page)
    else:
        page = min(b.page for b in own)
        left = min(b.bbox[0] for b in own if b.page == page)
        parent_left = min(b.bbox[0] for b in parent_own)
    report.record_test("child_left_of_parent", parent.kind, node.kind)
    if left < parent_left - INDENT_TOLERANCE:
        report.violations.append(
            Violation(
                check="child_left_of_parent",
                path=node.path,
                parent_path=parent.path,
                kind=node.kind,
                parent_kind=parent.kind,
                detail=f"left {left:.1f} is left of parent's {parent_left:.1f}",
                measure=parent_left - left,
                page=page,
            )
        )


def _check_own_above_first_child(node: Node, first: Node, report: InvariantReport) -> None:
    own = _own_boxes(node)
    child_own = _own_boxes(first) or first.bboxes_extent
    if not own or not child_own:
        return
    own_top = min((b.page, b.bbox[1]) for b in own)
    child_top = min((b.page, b.bbox[1]) for b in child_own)
    if own_top[0] > child_top[0] or (
        own_top[0] == child_top[0] and own_top[1] > child_top[1] + VERTICAL_TOLERANCE
    ):
        report.violations.append(
            Violation(
                check="own_box_below_first_child",
                path=node.path,
                parent_path=None,
                kind=node.kind,
                parent_kind=None,
                detail=(
                    f"own box starts at page {own_top[0]} y={own_top[1]:.1f}, "
                    f"first child {first.path} at page {child_top[0]} y={child_top[1]:.1f}"
                ),
                measure=own_top[1] - child_top[1],
            )
        )


def _check_sibling_order(first: Node, second: Node, report: InvariantReport) -> None:
    a = _own_boxes(first) or first.bboxes_extent
    b = _own_boxes(second) or second.bboxes_extent
    if not a or not b:
        return
    if first.kind == "cell" and second.kind == "cell":
        _check_cell_order(first, second, a, b, report)
        return
    a_end = max((x.page, x.bbox[3]) for x in a)
    b_start = min((x.page, x.bbox[1]) for x in b)
    report.record_test("siblings_out_of_reading_order", first.kind, second.kind)
    report.record_test("siblings_overlap_vertically", first.kind, second.kind)
    if b_start[0] < a_end[0]:
        report.violations.append(
            Violation(
                check="siblings_out_of_reading_order",
                path=second.path,
                parent_path=first.path,
                kind=second.kind,
                parent_kind=first.kind,
                detail=f"starts on page {b_start[0]}, previous sibling ends on page {a_end[0]}",
                measure=float(a_end[0] - b_start[0]),
            )
        )
        return
    if b_start[0] > a_end[0]:
        return          # different pages, nothing to compare vertically
    page = b_start[0]
    a_box = _page_box(a, page)
    b_box = _page_box(b, page)
    if a_box is None or b_box is None:
        return
    if b_box[1] < a_box[1] - VERTICAL_TOLERANCE:
        report.violations.append(
            Violation(
                check="siblings_out_of_reading_order",
                path=second.path,
                parent_path=first.path,
                kind=second.kind,
                parent_kind=first.kind,
                detail=f"top {b_box[1]:.1f} is above previous sibling's top {a_box[1]:.1f} on page {page}",
                measure=a_box[1] - b_box[1],
            )
        )
    elif b_box[1] < a_box[3] - sibling_overlap_tolerance(a_box, b_box):
        report.violations.append(
            Violation(
                check="siblings_overlap_vertically",
                path=second.path,
                parent_path=first.path,
                kind=second.kind,
                parent_kind=first.kind,
                detail=(
                    f"top {b_box[1]:.1f} overlaps previous sibling's bottom "
                    f"{a_box[3]:.1f} on page {page}"
                ),
                measure=a_box[3] - b_box[1],
            )
        )


def _check_cell_order(first: Node, second: Node, a, b, report: InvariantReport) -> None:
    """Cells read row-major: left to right within a row, rows top to bottom.

    Two cells of the same row share a vertical band by construction, so the
    stacked-sibling rule is the wrong one for them and would fire on every
    definition in the schedule.
    """
    report.record_test("siblings_out_of_reading_order", "cell", "cell")
    same_row = first.row is not None and first.row == second.row
    a_box = a[0].bbox
    b_box = b[0].bbox
    if same_row:
        if b_box[0] < a_box[0] - INDENT_TOLERANCE:
            report.violations.append(
                Violation(
                    check="siblings_out_of_reading_order",
                    path=second.path,
                    parent_path=first.path,
                    kind="cell",
                    parent_kind="cell",
                    detail=(
                        f"column {second.col} starts at x={b_box[0]:.1f}, left of "
                        f"column {first.col} at x={a_box[0]:.1f} in the same row"
                    ),
                    measure=a_box[0] - b_box[0],
                )
            )
        return
    if b[0].page < a[0].page:
        report.violations.append(
            Violation(
                check="siblings_out_of_reading_order",
                path=second.path,
                parent_path=first.path,
                kind="cell",
                parent_kind="cell",
                detail=f"row {second.row} starts on page {b[0].page}, before row {first.row} on page {a[0].page}",
                measure=float(a[0].page - b[0].page),
            )
        )
        return
    if b[0].page == a[0].page and b_box[1] < a_box[1] - sibling_overlap_tolerance(a_box, b_box):
        report.violations.append(
            Violation(
                check="siblings_out_of_reading_order",
                path=second.path,
                parent_path=first.path,
                kind="cell",
                parent_kind="cell",
                detail=(
                    f"row {second.row} starts at y={b_box[1]:.1f}, above row "
                    f"{first.row} at y={a_box[1]:.1f} on page {b[0].page}"
                ),
                measure=a_box[1] - b_box[1],
            )
        )


def _check_extent(node: Node, parent: Node, report: InvariantReport) -> None:
    report.record_test("extent_escapes_parent", parent.kind, node.kind)
    for box in node.bboxes_extent:
        outer = _page_box(parent.bboxes_extent, box.page)
        if outer is None:
            report.violations.append(
                Violation(
                    check="extent_escapes_parent",
                    path=node.path,
                    parent_path=parent.path,
                    kind=node.kind,
                    parent_kind=parent.kind,
                    detail=f"page {box.page} is in the child's extent but not the parent's",
                    measure=1.0,
                )
            )
            continue
        slack = max(
            outer[0] - box.bbox[0],
            outer[1] - box.bbox[1],
            box.bbox[2] - outer[2],
            box.bbox[3] - outer[3],
        )
        if slack > INDENT_TOLERANCE:
            report.violations.append(
                Violation(
                    check="extent_escapes_parent",
                    path=node.path,
                    parent_path=parent.path,
                    kind=node.kind,
                    parent_kind=parent.kind,
                    detail=f"extent on page {box.page} escapes the parent's by {slack:.1f}pt",
                    measure=slack,
                )
            )


def _explain(report: InvariantReport) -> None:
    """Label the violations that are explainable, leaving the rest to be seen.

    Two grounds, and only two. A page someone rendered and read is explained by
    what they saw. A violation firing uniformly across a whole level of a part
    is explained by the part's typesetting. Everything else stays unexplained
    and keeps the exit code honest.
    """
    for violation in report.violations:
        for verified in VERIFIED_TYPESETTING:
            if (
                verified.part == report.part
                and verified.check == violation.check
                and verified.page == violation.page
            ):
                violation.explained = (
                    f"geometry_is_document_typesetting: page {verified.page} rendered and "
                    f"read, {verified.seen}; the ink is genuinely left of the parent and "
                    f"the parse is correct"
                )
                break

    populations: dict[tuple[str, str, str], int] = {}
    for v in report.violations:
        if v.parent_kind is None:
            continue
        populations.setdefault((v.check, v.parent_kind, v.kind), 0)
        populations[(v.check, v.parent_kind, v.kind)] += 1

    totals = report.tested
    for v in report.violations:
        if v.parent_kind is None:
            continue
        key = (v.check, v.parent_kind, v.kind)
        hits = populations[key]
        total = totals.get(key, hits)
        if hits >= SYSTEMATIC_MIN_COUNT and total and hits / total >= SYSTEMATIC_SHARE:
            v.explained = (
                f"systematic_level_offset: {hits} of {total} {v.parent_kind}->{v.kind} pairs "
                f"in this part show the same {v.check}, so it is the part's typesetting "
                f"rather than a mis-parented node"
            )
