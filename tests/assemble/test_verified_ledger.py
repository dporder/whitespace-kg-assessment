"""The verified-typesetting ledger explains exactly what was looked at.

A ledger of human observations is only worth having if it cannot drift into a
rubber stamp. Two properties keep it honest: an entry explains the one node it
names and nothing else, and an entry that matches no violation is reported as
stale rather than sitting silently in the code.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.assemble import invariants as inv
from pipeline.schemas import BBox, Node, lineage_key, node_id

DOC, VER = "test", "v1"


def _node(path, kind, page, box, children=(), text=None, **extra):
    boxes = [BBox(page=page, bbox=box)] if box else []
    n = Node(
        id=node_id(DOC, VER, path),
        lineage_key=lineage_key(DOC, path),
        path=path,
        kind=kind,
        text=text,
        page_start=page,
        page_end=page,
        bboxes_own=boxes,
        bboxes_extent=list(boxes),
        order=0,
        children=list(children),
        **extra,
    )
    return n


def _set_extents(node: Node) -> None:
    """Extents are the union of own plus children, exactly as the builder makes
    them, so the only check this fixture can trip is the one under test."""
    for child in node.children:
        _set_extents(child)
    boxes = [b.bbox for b in node.bboxes_own]
    boxes += [b.bbox for c in node.children for b in c.bboxes_extent]
    if boxes:
        node.bboxes_extent = [
            BBox(
                page=node.page_start,
                bbox=(
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ),
            )
        ]


def _tree_with_two_offset_siblings():
    """Two items with identical measurements under one parent, on one page."""
    a = _node("p/1/a", "item", 7, (80.0, 120.0, 400.0, 134.0), text="first")
    b = _node("p/1/b", "item", 7, (80.0, 150.0, 400.0, 164.0), text="second")
    clause = _node("p/1", "clause", 7, (100.0, 100.0, 120.0, 114.0), [a, b])
    root = _node("p", "part", 7, None, [clause])
    _set_extents(root)
    return root


def test_an_entry_explains_only_the_node_it_names(monkeypatch):
    """Both siblings break the same check on the same page. The ledger names
    one of them, so exactly one is explained: a page-keyed ledger would have
    blanket-explained a node nobody rendered."""
    entry = inv.VerifiedRender(
        part="p", page=7, check="child_left_edge", path="p/1/a",
        seen="item (a) is printed left of its parent, verified by render",
    )
    monkeypatch.setattr(inv, "VERIFIED_TYPESETTING", (entry,))
    report = inv.check_tree("p", _tree_with_two_offset_siblings())

    hits = [v for v in report.violations if v.check == "child_left_edge"]
    assert {v.path for v in hits} == {"p/1/a", "p/1/b"}
    explained = [v for v in hits if v.explained]
    assert [v.path for v in explained] == ["p/1/a"]
    assert explained[0].explained.startswith("child_left_edge_document_typesetting")
    assert [v.path for v in report.unexplained] == ["p/1/b"]


def test_a_stale_entry_is_reported_never_silently_ignored(monkeypatch):
    """An entry whose node no longer violates anything is surfaced. The parse
    may have improved under it, or the node may have moved."""
    stale = inv.VerifiedRender(
        part="p", page=7, check="child_left_edge", path="p/1/gone",
        seen="a node that no longer exists",
    )
    monkeypatch.setattr(inv, "VERIFIED_TYPESETTING", (stale,))
    report = inv.check_tree("p", _tree_with_two_offset_siblings())

    reported = inv.stale_ledger_entries([report])
    assert len(reported) == 1
    assert reported[0]["path"] == "p/1/gone"
    assert "matched no violation" in reported[0]["note"]


def test_staleness_is_scoped_to_the_parts_this_run_assembled(monkeypatch):
    """A batch run says nothing about entries for parts it never looked at."""
    other = inv.VerifiedRender(
        part="some-other-part", page=99, check="child_left_edge", path="other/1/a",
        seen="not in this run's scope",
    )
    monkeypatch.setattr(inv, "VERIFIED_TYPESETTING", (other,))
    report = inv.check_tree("p", _tree_with_two_offset_siblings())
    assert inv.stale_ledger_entries([report]) == []


def test_the_shipped_ledger_names_a_path_for_every_entry():
    """No entry may be page-only: the path is what stops it generalising."""
    assert inv.VERIFIED_TYPESETTING, "the ledger records real observations"
    for entry in inv.VERIFIED_TYPESETTING:
        assert entry.path and entry.part and entry.check and entry.page
        assert entry.path.startswith(entry.part), (entry.path, entry.part)
        assert entry.seen.strip(), "an entry must say what was seen"
