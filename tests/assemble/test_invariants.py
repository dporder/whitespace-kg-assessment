"""The geometric invariants: each check fires on a tree that breaks it, stays
quiet on one that does not, and the systematic explanation separates typesetting
from mis-parenting.

Synthetic trees, because an invariant test that can only be exercised by finding
the right page in a 475-page PDF is a test of that PDF rather than of the check.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.assemble.invariants import (
    SYSTEMATIC_MIN_COUNT,
    check_tree,
    Violation,
)
from pipeline.assemble.tree import build_part, renumber
from pipeline.schemas import BBox, Node, content_hash, lineage_key, node_id

DOC, VER = "test", "v1"


def _node(path, kind, box, children=(), text=None, **extra):
    boxes = [BBox(page=box[0], bbox=box[1])] if box else []
    node = Node(
        id=node_id(DOC, VER, path),
        lineage_key=lineage_key(DOC, path),
        content_hash=content_hash(text) if text is not None else None,
        path=path,
        kind=kind,
        text=text,
        page_start=box[0] if box else 1,
        page_end=box[0] if box else 1,
        bboxes_own=boxes,
        bboxes_extent=list(boxes),
        order=0,
        children=list(children),
        **extra,
    )
    node.bboxes_extent = _extent(node)
    return node


def _extent(node: Node) -> list[BBox]:
    by_page: dict[int, list] = {}
    for box in node.bboxes_own:
        by_page.setdefault(box.page, []).append(box.bbox)
    for child in node.children:
        for box in child.bboxes_extent:
            by_page.setdefault(box.page, []).append(box.bbox)
    return [
        BBox(
            page=page,
            bbox=(
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ),
        )
        for page, boxes in sorted(by_page.items())
    ]


def _checks(report, name):
    return [v for v in report.violations if v.check == name]


def test_a_clean_tree_reports_nothing():
    child_a = _node("p/1/a", "item", (1, (110.0, 120.0, 400.0, 134.0)), text="first")
    child_b = _node("p/1/b", "item", (1, (110.0, 140.0, 400.0, 154.0)), text="second")
    clause = _node("p/1", "clause", (1, (100.0, 100.0, 120.0, 114.0)), [child_a, child_b])
    root = _node("p", "part", None, [clause])
    report = check_tree("p", root)
    assert report.violations == []


def test_a_child_left_of_its_parent_is_reported():
    child = _node("p/1/a", "item", (1, (80.0, 120.0, 400.0, 134.0)), text="left of parent")
    clause = _node("p/1", "clause", (1, (100.0, 100.0, 120.0, 114.0)), [child])
    root = _node("p", "part", None, [clause])
    report = check_tree("p", root)
    hits = _checks(report, "child_left_of_parent")
    assert [v.path for v in hits] == ["p/1/a"]
    assert hits[0].measure == pytest.approx(20.0)


def test_a_parent_below_its_first_child_is_reported():
    child = _node("p/1/a", "item", (1, (110.0, 50.0, 400.0, 64.0)), text="above the parent")
    clause = _node("p/1", "clause", (1, (100.0, 100.0, 120.0, 114.0)), [child])
    root = _node("p", "part", None, [clause])
    report = check_tree("p", root)
    assert [v.path for v in _checks(report, "own_box_below_first_child")] == ["p/1"]


def test_siblings_out_of_order_and_overlapping_are_told_apart():
    up = _node("p/1/b", "item", (1, (110.0, 90.0, 400.0, 104.0)), text="starts above its predecessor")
    first = _node("p/1/a", "item", (1, (110.0, 120.0, 400.0, 134.0)), text="first")
    clause = _node("p/1", "clause", (1, (100.0, 100.0, 120.0, 114.0)), [first, up])
    root = _node("p", "part", None, [clause])
    assert [v.path for v in _checks(check_tree("p", root), "siblings_out_of_reading_order")] == ["p/1/b"]

    overlap = _node("p/2/b", "item", (1, (110.0, 128.0, 400.0, 142.0)), text="overlaps by most of a line")
    first2 = _node("p/2/a", "item", (1, (110.0, 120.0, 400.0, 140.0)), text="first")
    clause2 = _node("p/2", "clause", (1, (100.0, 100.0, 120.0, 114.0)), [first2, overlap])
    root2 = _node("p", "part", None, [clause2])
    assert [v.path for v in _checks(check_tree("p", root2), "siblings_overlap_vertically")] == ["p/2/b"]


def test_font_metric_overlap_between_siblings_is_not_a_violation():
    """Line boxes span the font's full ascent and descent, so consecutive lines
    overlap by a point or two by construction."""
    first = _node("p/1/a", "item", (1, (110.0, 120.0, 400.0, 136.0)), text="first")
    second = _node("p/1/b", "item", (1, (110.0, 134.5, 400.0, 150.5)), text="second")
    clause = _node("p/1", "clause", (1, (100.0, 100.0, 120.0, 114.0)), [first, second])
    root = _node("p", "part", None, [clause])
    assert _checks(check_tree("p", root), "siblings_overlap_vertically") == []


def test_cells_of_one_row_are_not_stacked_siblings():
    """Two cells in the same row share a vertical band on purpose."""
    left = _node("p/t/0/0", "cell", (1, (72.0, 140.0, 175.0, 156.0)), text="term",
                 row=0, col=0, cell_role="label", role_confidence=0.99)
    right = _node("p/t/0/1", "cell", (1, (180.0, 140.0, 576.0, 156.0)), text="definition",
                  row=0, col=1, cell_role="value", role_confidence=0.99)
    table = _node("p/t", "table", None, [left, right], n_rows=1, n_cols=2)
    root = _node("p", "part", None, [table])
    report = check_tree("p", root)
    assert report.violations == []


def test_cells_in_the_wrong_column_order_are_reported():
    left = _node("p/t/0/0", "cell", (1, (300.0, 140.0, 400.0, 156.0)), text="term",
                 row=0, col=0, cell_role="label", role_confidence=0.99)
    right = _node("p/t/0/1", "cell", (1, (72.0, 140.0, 175.0, 156.0)), text="definition",
                  row=0, col=1, cell_role="value", role_confidence=0.99)
    table = _node("p/t", "table", None, [left, right], n_rows=1, n_cols=2)
    root = _node("p", "part", None, [table])
    assert [v.path for v in _checks(check_tree("p", root), "siblings_out_of_reading_order")] == ["p/t/0/1"]


def test_a_level_wide_offset_is_explained_and_an_isolated_one_is_not():
    """Every clause under a heading sitting three points left of it is the
    part's typesetting. One clause out of many sitting there is a mis-parse."""
    children = [
        _node(f"p/1/1.{i}", "clause", (1, (27.0, 100.0 + 20 * i, 400.0, 114.0 + 20 * i)), text=f"c{i}")
        for i in range(SYSTEMATIC_MIN_COUNT + 2)
    ]
    heading = _node("p/1", "heading", (1, (30.4, 80.0, 300.0, 96.0)), children)
    root = _node("p", "part", None, [heading])
    report = check_tree("p", root)
    hits = _checks(report, "child_left_of_parent")
    assert len(hits) == len(children)
    assert all(v.explained and v.explained.startswith("systematic_level_offset") for v in hits)
    assert report.unexplained == []

    aligned = [
        _node(f"p/2/2.{i}", "clause", (1, (30.4, 100.0 + 20 * i, 400.0, 114.0 + 20 * i)), text=f"c{i}")
        for i in range(6)
    ]
    stray = _node("p/2/2.9", "clause", (1, (10.0, 260.0, 400.0, 274.0)), text="stray")
    heading2 = _node("p/2", "heading", (1, (30.4, 80.0, 300.0, 96.0)), aligned + [stray])
    root2 = _node("p", "part", None, [heading2])
    report2 = check_tree("p", root2)
    assert [v.path for v in report2.unexplained] == ["p/2/2.9"]


def test_the_report_serialises_counts_and_locations():
    child = _node("p/1/a", "item", (1, (80.0, 120.0, 400.0, 134.0)), text="left of parent")
    clause = _node("p/1", "clause", (1, (100.0, 100.0, 120.0, 114.0)), [child])
    root = _node("p", "part", None, [clause])
    data = check_tree("p", root).as_json()
    assert data["part"] == "p"
    assert data["total"] == 1
    assert data["unexplained"] == 1
    assert data["by_check"]["child_left_of_parent"] == 1
    assert data["violations"][0]["path"] == "p/1/a"
    assert "left of parent's" in data["violations"][0]["detail"]


def test_real_core_terms_has_no_unexplained_violations(core_terms_layout, profile):
    """The definition-of-done gate for stages 1 and 2: on Core Terms the
    invariant report shows zero unexplained violations, with the explained ones
    listed rather than hidden."""
    root, _ = build_part(core_terms_layout, profile)
    renumber(root)
    report = check_tree("core-terms", Node.model_validate(root.model_dump()))
    assert report.unexplained == [], [v.as_json() for v in report.unexplained]
    assert report.violations, "the explained ones are reported, not suppressed"
