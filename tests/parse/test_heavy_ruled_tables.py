"""Definition blocks drawn with a heavy border are still tables.

The regression this pins: rules were recognised by thickness alone, capped at
2.0pt. Joint Schedule 1 draws its definition tables with 0.6pt hairlines and
parsed fine; Call-Off Schedule 9 draws the same shape with a 3.0pt outer
border, so only its 0.6pt middle rule survived the filter. One vertical rule is
not a grid, the block fell back to the prose path, and merging by vertical
overlap interleaved the two columns into

    "Breach of means the occurrence of: Security"

which is how a term column and a definition column read when they are zipped
together line by line. Rendering page 346 settled that the source is a clean
ruled table and the interleaving was ours.

Call-Off Schedule 9 defines four terms of its own across its two parts, which
SPEC section 4 and DESIGN both record, and only one of them used to survive.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from pipeline.parse.blocks import build_blocks, collect_page
from pipeline.parse.numbering import Rulebook
from pipeline.parse.tables import page_grids


@pytest.fixture(scope="module")
def cos9_blocks(cos9_scan):
    part = cos9_scan.part_by_id("call-off-schedule-9")
    assert part is not None
    doc = pymupdf.open(config.PDF)
    inputs = [
        collect_page(doc[p - 1], p, cos9_scan.pages[p].furniture.body)
        for p in range(part.page_start, part.page_end + 1)
    ]
    rb = Rulebook(config.DEFAULT_PROFILE, config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE])
    blocks = build_blocks(inputs, rb)
    doc.close()
    return blocks


def test_a_three_point_border_is_still_a_rule():
    """The outer border of these tables is 3.0pt; the middle rule is 0.6pt.
    Both are rules, and the square corner joins the pack draws at each border
    intersection are not."""
    doc = pymupdf.open(config.PDF)
    grids = page_grids(doc[345], 346)
    doc.close()
    assert len(grids) == 1
    grid = grids[0]
    assert grid.n_cols == 2, "term column and definition column"
    assert [round(c) for c in grid.cols] == [122, 236, 524]


def test_both_parts_carry_their_definitions_as_tables(cos9_blocks):
    tables = [b for b in cos9_blocks if b.block_kind == "table"]
    assert len(tables) == 2, "Part A and Part B each open with a definitions block"
    assert [t.page_start for t in tables] == [340, 346]
    assert all(t.table_cols == 2 for t in tables)


def test_the_four_part_local_terms_are_label_cells(cos9_blocks):
    """Verbatim, quotation marks and all."""
    tables = [b for b in cos9_blocks if b.block_kind == "table"]
    part_a = [c.text for c in tables[0].cells if c.col == 0 and c.text.strip()]
    part_b = [c.text for c in tables[1].cells if c.col == 0 and c.text.strip()]

    assert part_a == ['"Breach of Security"', '"Security Management Plan"']
    assert part_b == ['"Breach of Security"', '"ISMS"', '"Security Tests"']

    distinct = sorted(set(part_a) | set(part_b))
    assert distinct == [
        '"Breach of Security"',
        '"ISMS"',
        '"Security Management Plan"',
        '"Security Tests"',
    ]
    assert len(distinct) == 4, "the four terms Call-Off Schedule 9 defines of its own"


def test_the_columns_are_not_interleaved(cos9_blocks):
    """The exact string the enrichment builder saw must not exist anywhere."""
    for block in cos9_blocks:
        assert "Breach of means" not in block.text, block.text[:120]
        for cell in block.cells:
            assert "Breach of means" not in cell.text, cell.text[:120]


def test_each_term_pairs_with_its_own_definition(cos9_blocks):
    tables = [b for b in cos9_blocks if b.block_kind == "table"]
    rows: dict[tuple[int, int], dict[int, str]] = {}
    for ti, table in enumerate(tables):
        for cell in table.cells:
            rows.setdefault((ti, cell.row), {})[cell.col] = cell.text
    breach = [r for r in rows.values() if r.get(0) == '"Breach of Security"']
    assert len(breach) == 2, "defined once in each part"
    for row in breach:
        assert "occurrence of" in row[1], row[1][:80]
    ismses = [r for r in rows.values() if r.get(0) == '"ISMS"']
    assert ismses and ismses[0][1].startswith("the information security management system")


def test_the_definitions_block_is_no_longer_swallowed_by_the_lead_in(cos9_blocks):
    """Clause 1.1 introduces the block; it must not contain it."""
    # Both definitions lead-ins are numbered 1.1, and so is the Annex's opening
    # paragraph about classified information, which is a different provision.
    # The two that matter are the ones introducing the tables.
    lead_ins = [
        b for b in cos9_blocks
        if b.block_kind == "numbered" and b.number == "1.1" and b.page_start in (340, 346)
    ]
    assert len(lead_ins) == 2, "both parts number their definitions lead-in 1.1"
    for block in lead_ins:
        assert block.text.rstrip().endswith(
            "they shall supplement Joint Schedule 1 (Definitions):"
        ), block.text[-90:]
        assert "Breach of Security" not in block.text
