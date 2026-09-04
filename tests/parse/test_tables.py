"""Tables and form rows, parsed from the page's own ruled grid.

The two shapes the spec calls out are pinned here: the Award Form's numbered
label and value rows with their placeholders and stray-character typo, and the
definitions schedule's two-column layout whose quoted terms wrap mid-cell.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from pipeline.parse.blocks import build_blocks, collect_page
from pipeline.parse.numbering import Rulebook
from pipeline.parse.tables import fill_cells, page_grids
from pipeline.parse.words import page_words


def _rulebook():
    return Rulebook(config.DEFAULT_PROFILE, config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE])


def _blocks(scan, part_id):
    part = scan.part_by_id(part_id)
    doc = pymupdf.open(config.PDF)
    inputs = [
        collect_page(doc[p - 1], p, scan.pages[p].furniture.body)
        for p in range(part.page_start, part.page_end + 1)
    ]
    blocks = build_blocks(inputs, _rulebook())
    doc.close()
    return blocks


def test_award_form_grid_is_three_columns_of_row_number_label_value(core_terms_scan):
    doc = pymupdf.open(config.PDF)
    grids = page_grids(doc[23], 24)
    doc.close()
    assert len(grids) == 1
    grid = grids[0]
    assert grid.n_cols == 3
    assert [round(c) for c in grid.cols] == [30, 52, 154, 557]


def test_a_line_crossing_a_column_rule_is_split_at_it(core_terms_scan):
    """"1. CCS" runs from x=36 to x=89.7 across the rule at x=51.8, so the row
    number belongs to the first column and the label to the second."""
    doc = pymupdf.open(config.PDF)
    page = doc[23]
    grid = page_grids(page, 24)[0]
    cells, _ = fill_cells(grid, core_terms_scan.pages[24].furniture.body, page_words(page))
    doc.close()
    by_slot = {(c.row, c.col): c.text for c in cells}
    assert by_slot[(0, 0)] == "1."
    assert by_slot[(0, 1)] == "CCS"
    assert by_slot[(0, 2)].startswith("The Minister for the Cabinet Office")


def test_award_form_rows_keep_placeholders_and_the_typo_verbatim(core_terms_scan):
    blocks = _blocks(core_terms_scan, "award-form")
    tables = [b for b in blocks if b.block_kind == "table"]
    assert tables, "the Award Form is a ruled form, not prose"
    cells = [c for b in tables for c in b.cells]

    label = next(c for c in cells if c.text == "rFramework Contract")
    assert label.role == "label"
    assert any(a.startswith("stray_character_in_label") for a in label.anomalies)
    assert "rFramework" in label.text, "recorded verbatim, never corrected"

    placeholders = [c.text for c in cells if c.text.startswith("[Insert")]
    assert placeholders, "the form's [Insert ...] placeholders are preserved"


def test_form_rows_read_their_sub_labels_before_their_values(core_terms_scan):
    """Inside a cell, reading order is by row then left to right. "Name:" at
    x=161.4, y=267.3 comes before "[Insert name...]" at x=280.0, y=266.9."""
    blocks = _blocks(core_terms_scan, "award-form")
    cells = [c for b in blocks if b.block_kind == "table" for c in b.cells]
    supplier = next(c for c in cells if c.text.startswith("Name:"))
    assert supplier.text.startswith("Name: [Insert name (registered name if registered)]")


def test_definitions_are_two_columns_with_terms_wrapping_mid_cell(definitions_scan):
    blocks = _blocks(definitions_scan, "joint-schedule-1")
    tables = [b for b in blocks if b.block_kind == "table"]
    assert tables
    assert tables[0].table_cols == 2

    terms = {c.text: c for b in tables for c in b.cells if c.col == 0 and c.text}
    # Wrapped across two lines inside one cell, joined by box geometry.
    assert "Accounting Reference Date" in terms
    assert terms["Accounting Reference Date"].role == "label"
    # Wrapped and missing its opening quotation mark and first letter on the
    # page itself, recorded exactly as the ink reads.
    assert 'Additional nsurances"' in terms
    assert any(
        a.startswith("unpaired_closing_quote_in_cell")
        for a in terms['Additional nsurances"'].anomalies
    )


def test_definition_values_pair_with_their_terms(definitions_scan):
    blocks = _blocks(definitions_scan, "joint-schedule-1")
    table = next(b for b in blocks if b.block_kind == "table")
    rows = {}
    for cell in table.cells:
        rows.setdefault(cell.row, {})[cell.col] = cell.text
    row = next(r for r in rows.values() if r.get(0) == "Accounting Reference Date")
    assert row[1].startswith("means in each year the date")


def test_a_table_continuing_over_a_page_break_is_one_table(definitions_scan):
    blocks = _blocks(definitions_scan, "joint-schedule-1")
    table = next(b for b in blocks if b.block_kind == "table")
    assert table.page_end > table.page_start
    assert any(a.startswith("table_stitched_across_pages") for a in table.anomalies)
