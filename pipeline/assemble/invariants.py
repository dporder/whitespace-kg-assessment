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

# The canonical check ids from SPEC 2.1. Shared contract: stage 8 matches an
# explanation to a violation by testing whether the anomaly key starts with the
# check id, so these names are not ours to choose locally. Two stages named the
# same checks differently once and their reports disagreed over identical
# trees; the vocabulary is pinned in the spec to stop that recurring.
CHECKS = (
    "child_left_edge",
    "own_box_above_first_child",
    "sibling_overlap",
    "siblings_ascend",
    "extent_nests",
)

# An explanation anomaly is keyed `<check_id>_<reason>`. An unexplained
# violation is stamped too, so a reviewer reading the tree sees it, but under a
# key that CANNOT be mistaken for an explanation: a bare "child_left_edge: ..."
# would start with the check id and stage 8 would count the complaint as its own
# excuse, turning every violation into a silent pass.
UNEXPLAINED_PREFIX = "unresolved"

# A violation is systematic when it fires on this share of the comparable pairs
# at the same (part, parent kind, child kind), over at least this many pairs.
# Below either bar it is isolated and stays unexplained.
SYSTEMATIC_SHARE = 0.8
SYSTEMATIC_MIN_COUNT = 3


@dataclass(frozen=True)
class VerifiedRender:
    """One node whose geometry was checked by rendering its page and reading it.

    A violation the parser cannot explain from the numbers alone is not
    necessarily a mis-parse; sometimes the page really is set that way. The only
    way to tell is to look, so each entry records a node that was looked at and
    what was seen.

    Keyed on the node path, not just the page. A page key would blanket-explain
    any later violation that happened to land on the same page with prose about
    a node nobody rendered, which is how a ledger quietly becomes a rubber
    stamp. Two sibling items with identical measurements on adjacent pages are
    two separate observations, and only the one that was seen is explained.
    """
    part: str
    page: int
    check: str
    path: str
    seen: str


# Nodes whose pages were rendered and read during the parser build. Each shows
# the document printing that child further left than the parent above it, which
# is the pack's house style for hanging-indented sub-paragraphs, not a
# mis-parented node. Only nodes actually looked at appear here: Framework
# Schedule 7's item (b) under 3.1.4 measures identically to its sibling (a) but
# sits on the next page, was not rendered, and stays unexplained.
_VERIFIED_ENTRIES: tuple[VerifiedRender, ...] = (
    VerifiedRender(
        part="call-off-schedule-9", page=344, check="child_left_edge",
        path="call-off-schedule-9/5/5.2/5.2.1",
        seen="paragraph 5.2.1 is printed at the left margin, left of its parent 5.2",
    ),
    VerifiedRender(
        part="call-off-schedule-9", page=347, check="child_left_edge",
        path="call-off-schedule-9/part-b-long-form-security-requirements/2/2.3/2.3.1",
        seen="paragraph 2.3.1 is printed left of its parent 2.3",
    ),
    VerifiedRender(
        part="call-off-schedule-9", page=347, check="child_left_edge",
        path="call-off-schedule-9/part-b-long-form-security-requirements/2/2.3/2.3.2",
        seen="paragraph 2.3.2 is printed left of its parent 2.3",
    ),
    VerifiedRender(
        part="framework-schedule-7", page=95, check="child_left_edge",
        path="framework-schedule-7/3/3.1/3.1.4/a",
        seen="item (a) hangs left of the number 3.1.4 above it",
    ),
) + tuple(
    VerifiedRender(
        part="framework-schedule-7", page=97, check="child_left_edge",
        path=f"framework-schedule-7/3/3.2/{parent}/{letter}",
        seen=f"item ({letter}) hangs left of the number {parent} above it",
    )
    for parent, letters in (("3.2.2", "abcde"), ("3.2.4", "ab"))
    for letter in letters
)
VERIFIED_TYPESETTING: tuple[VerifiedRender, ...] = _VERIFIED_ENTRIES


def stale_ledger_entries(reports: "list[InvariantReport]") -> list[dict]:
    """Ledger entries that matched no violation in this run.

    Reported, never silently ignored. A stale entry means the tree changed
    under a human observation: either the parse improved and the violation is
    gone, or a node moved and the entry now explains nothing. Both are things
    someone should look at, and an unnoticed stale entry is an explanation
    waiting to attach itself to the wrong node later.
    """
    matched = {
        (r.part, v.check, v.path, v.page)
        for r in reports
        for v in r.violations
    }
    # Only parts this run actually assembled can make an entry stale. A run
    # scoped to one batch says nothing about the entries for parts it never
    # looked at, and calling those stale would cry wolf on every batch run.
    in_scope = {r.part for r in reports}
    return [
        {
            "part": e.part, "check": e.check, "path": e.path, "page": e.page,
            "seen": e.seen,
            "note": "ledger entry matched no violation in this run; the parse may have "
                    "changed under it, or the node may have moved",
        }
        for e in VERIFIED_TYPESETTING
        if e.part in in_scope and (e.part, e.check, e.path, e.page) not in matched
    ]


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
    # Comparisons the checker declined to make, with the reason. A check that
    # silently skips looks identical in the numbers to a check that passed.
    skipped: list[dict] = field(default_factory=list)

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
            "skipped": self.skipped,
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
    own = _own_boxes(node)
    parent_own = _own_boxes(parent)
    if not own or not parent_own:
        # Falling back to a parent's extent made this check vacuous: the extent
        # already contains the child, so the comparison could never fail. A
        # container with no ink of its own is not compared at all, and the skip
        # is recorded so the report says what it did not measure rather than
        # implying it measured and passed.
        report.skipped.append(
            {
                "check": "child_left_edge",
                "path": node.path,
                "parent_path": parent.path,
                "reason": "no own boxes on the child" if not own else
                          "parent holds no ink of its own to compare an indent against",
            }
        )
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
    report.record_test("child_left_edge", parent.kind, node.kind)
    if left < parent_left - INDENT_TOLERANCE:
        report.violations.append(
            Violation(
                check="child_left_edge",
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
                check="own_box_above_first_child",
                path=node.path,
                parent_path=None,
                kind=node.kind,
                parent_kind=None,
                detail=(
                    f"own box starts at page {own_top[0]} y={own_top[1]:.1f}, "
                    f"first child {first.path} at page {child_top[0]} y={child_top[1]:.1f}"
                ),
                measure=own_top[1] - child_top[1],
                page=own_top[0],
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
    report.record_test("siblings_ascend", first.kind, second.kind)
    report.record_test("sibling_overlap", first.kind, second.kind)
    if b_start[0] < a_end[0]:
        report.violations.append(
            Violation(
                check="siblings_ascend",
                path=second.path,
                parent_path=first.path,
                kind=second.kind,
                parent_kind=first.kind,
                detail=f"starts on page {b_start[0]}, previous sibling ends on page {a_end[0]}",
                measure=float(a_end[0] - b_start[0]),
                page=b_start[0],
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
                check="siblings_ascend",
                path=second.path,
                parent_path=first.path,
                kind=second.kind,
                parent_kind=first.kind,
                detail=f"top {b_box[1]:.1f} is above previous sibling's top {a_box[1]:.1f} on page {page}",
                measure=a_box[1] - b_box[1],
                page=page,
            )
        )
    elif b_box[1] < a_box[3] - sibling_overlap_tolerance(a_box, b_box):
        report.violations.append(
            Violation(
                check="sibling_overlap",
                path=second.path,
                parent_path=first.path,
                kind=second.kind,
                parent_kind=first.kind,
                detail=(
                    f"top {b_box[1]:.1f} overlaps previous sibling's bottom "
                    f"{a_box[3]:.1f} on page {page}"
                ),
                measure=a_box[3] - b_box[1],
                page=page,
            )
        )


def _check_cell_order(first: Node, second: Node, a, b, report: InvariantReport) -> None:
    """Cells read row-major: left to right within a row, rows top to bottom.

    Two cells of the same row share a vertical band by construction, so the
    stacked-sibling rule is the wrong one for them and would fire on every
    definition in the schedule.
    """
    report.record_test("siblings_ascend", "cell", "cell")
    same_row = first.row is not None and first.row == second.row
    a_box = a[0].bbox
    b_box = b[0].bbox
    if same_row:
        # Two cells of one row share a vertical band by construction: they sit
        # side by side, so a stacked-sibling overlap rule can never be satisfied
        # by them. Recorded as a violation and explained on the spot rather than
        # skipped, because a reader comparing this report against one that does
        # apply the stacked rule to cells needs to see the same pair counted and
        # be told why it is not evidence of anything.
        report.record_test("sibling_overlap", "cell", "cell")
        if b_box[1] < a_box[3] - sibling_overlap_tolerance(a_box, b_box):
            report.violations.append(
                Violation(
                    check="sibling_overlap",
                    path=second.path,
                    parent_path=first.path,
                    kind="cell",
                    parent_kind="cell",
                    detail=(
                        f"column {second.col} shares row {second.row} with column "
                        f"{first.col} and so shares its vertical band"
                    ),
                    measure=a_box[3] - b_box[1],
                    page=a[0].page,
                    explained=(
                        "sibling_overlap_cells_share_a_row: these two cells are columns "
                        f"{first.col} and {second.col} of row {second.row}, side by side "
                        "rather than stacked, so vertical overlap between them is the "
                        "table's shape and not a mis-ordered sibling"
                    ),
                )
            )
        if b_box[0] < a_box[0] - INDENT_TOLERANCE:
            report.violations.append(
                Violation(
                    check="siblings_ascend",
                    path=second.path,
                    parent_path=first.path,
                    kind="cell",
                    parent_kind="cell",
                    detail=(
                        f"column {second.col} starts at x={b_box[0]:.1f}, left of "
                        f"column {first.col} at x={a_box[0]:.1f} in the same row"
                    ),
                    measure=a_box[0] - b_box[0],
                    page=a[0].page,
                )
            )
        return
    if b[0].page < a[0].page:
        report.violations.append(
            Violation(
                check="siblings_ascend",
                path=second.path,
                parent_path=first.path,
                kind="cell",
                parent_kind="cell",
                detail=f"row {second.row} starts on page {b[0].page}, before row {first.row} on page {a[0].page}",
                measure=float(a[0].page - b[0].page),
                page=b[0].page,
            )
        )
        return
    if b[0].page == a[0].page and b_box[1] < a_box[1] - sibling_overlap_tolerance(a_box, b_box):
        report.violations.append(
            Violation(
                check="siblings_ascend",
                path=second.path,
                parent_path=first.path,
                kind="cell",
                parent_kind="cell",
                detail=(
                    f"row {second.row} starts at y={b_box[1]:.1f}, above row "
                    f"{first.row} at y={a_box[1]:.1f} on page {b[0].page}"
                ),
                measure=a_box[1] - b_box[1],
                page=b[0].page,
            )
        )


def _check_extent(node: Node, parent: Node, report: InvariantReport) -> None:
    # One test per page compared, because this check can produce one violation
    # per page. Counting a single test for a node that spans five pages made the
    # systematic-offset ratio compare five hits against one comparison.
    for box in node.bboxes_extent:
        report.record_test("extent_nests", parent.kind, node.kind)
        outer = _page_box(parent.bboxes_extent, box.page)
        if outer is None:
            report.violations.append(
                Violation(
                    check="extent_nests",
                    path=node.path,
                    parent_path=parent.path,
                    kind=node.kind,
                    parent_kind=parent.kind,
                    detail=f"page {box.page} is in the child's extent but not the parent's",
                    measure=1.0,
                    page=box.page,
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
                    check="extent_nests",
                    path=node.path,
                    parent_path=parent.path,
                    kind=node.kind,
                    parent_kind=parent.kind,
                    detail=f"extent on page {box.page} escapes the parent's by {slack:.1f}pt",
                    measure=slack,
                    page=box.page,
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
                and verified.path == violation.path
                and verified.page == violation.page
            ):
                violation.explained = (
                    f"{violation.check}_document_typesetting: page {verified.page} rendered "
                    f"and read, {verified.seen}; the ink is genuinely set that way and the "
                    f"parse is correct"
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
        # Default 0, never `hits`. Falling back to the hit count would make a
        # missing denominator read as "all of them failed", so a check whose
        # comparisons were never counted would auto-explain every violation it
        # produced. A missing denominator explains nothing.
        total = totals.get(key, 0)
        if hits >= SYSTEMATIC_MIN_COUNT and total and hits / total >= SYSTEMATIC_SHARE:
            v.explained = (
                f"{v.check}_systematic_offset: {hits} of {total} {v.parent_kind}->{v.kind} "
                f"pairs in this part fail the same way, so it is the part's typesetting "
                f"rather than a mis-parented node"
            )
