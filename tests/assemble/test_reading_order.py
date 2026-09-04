"""Prose runs take their true reading-order position among their siblings.

The regression this pins: every prose run used to be inserted at index 0, so a
container with several of them stacked them backwards and `order` walked the
last page before the first. Framework Schedule 7's Part 2 is the case that
caught it, and the geometric invariants caught it first — the sibling ordering
check was right and the builder was wrong.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from pipeline.assemble.invariants import check_tree
from pipeline.assemble.tree import build_part, renumber
from pipeline.parse.document import scan
from pipeline.parse.layout import build_layout
from pipeline.parse.numbering import Rulebook
from pipeline.schemas import Node


@pytest.fixture(scope="module")
def fs7() -> Node:
    document = scan(config.PDF, config.BATCHES, page_range=(94, 106))
    rulebook = Rulebook(config.DEFAULT_PROFILE, config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE])
    part = document.part_by_id("framework-schedule-7")
    assert part is not None
    layout = build_layout(
        config.PDF, document, part, rulebook, config.DOCUMENT_ID, config.BATCHES
    ).as_json()
    root, _ = build_part(layout, config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE])
    renumber(root)
    return Node.model_validate(root.model_dump())


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def test_siblings_are_in_page_order_everywhere(fs7):
    for node in _walk(fs7):
        anatomy = [c for c in node.children if c.kind != "ref"]
        pages = [c.page_start for c in anatomy]
        assert pages == sorted(pages), (
            f"{node.path} orders its children {pages}, which is not reading order"
        )


def test_order_is_preorder_and_ascends_with_the_page(fs7):
    seen = [(n.order, n.page_start) for n in _walk(fs7)]
    assert [o for o, _ in seen] == list(range(len(seen))), "order must be dense preorder"
    # Within one part, a later node never starts on an earlier page than the
    # node before it in reading order.
    pages = [p for _, p in seen]
    assert pages == sorted(pages), "preorder walk goes backwards through the document"


def test_the_part_2_container_reads_forwards(fs7):
    """The specific container that stacked backwards."""
    by_path = {n.path: n for n in _walk(fs7)}
    target = next(
        (n for p, n in by_path.items() if p.endswith("part-2-award-criteria/3")), None
    )
    assert target is not None, "Framework Schedule 7 Part 2 paragraph 3 not found"
    anatomy = [c for c in target.children if c.kind != "ref"]
    assert len(anatomy) > 1, "the regression needs a container with several children"
    assert [c.page_start for c in anatomy] == sorted(c.page_start for c in anatomy)
    assert [c.order for c in anatomy] == sorted(c.order for c in anatomy)


def test_intro_suffixes_number_by_sequence(fs7):
    """intro, intro-2, intro-3 in document order, not by sibling count."""
    for node in _walk(fs7):
        intros = [c for c in node.children if c.kind == "intro"]
        if len(intros) < 2:
            continue
        suffixes = [c.path.rsplit("/", 1)[-1] for c in intros]
        assert suffixes[0] == "intro"
        assert suffixes == ["intro"] + [f"intro-{i}" for i in range(2, len(intros) + 1)]


def test_no_sibling_ordering_violations_remain(fs7):
    report = check_tree("framework-schedule-7", fs7)
    ordering = [
        v for v in report.violations
        if v.check == "siblings_ascend" and v.explained is None
    ]
    assert ordering == [], [v.as_json() for v in ordering]
