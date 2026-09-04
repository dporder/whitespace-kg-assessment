"""The provided artifacts: the notes' page map and the PDF's embedded outline.

Both are stage 8 inputs only (SPEC section 4) and both are read at runtime from
the assignment directory, never copied into the repo. The parser tests use an
inline notes file so they hold on any machine; the tests that assert the real
document's numbers skip when the assignment is not present.
"""
from __future__ import annotations

import pytest

import config
from pipeline.eval import provided


@pytest.mark.parametrize("name,expected", [
    ("Core terms", "core-terms"),
    ("Framework Award Form", "award-form"),
    ("Framework Schedule 1 - Specification", "framework-schedule-1"),
    ("Joint Schedule 1 - Definitions", "joint-schedule-1"),
    ("Joint Schedule 11 Processing Data", "joint-schedule-11"),
    ("Joint Schedule 7  - Financial Difficulties", "joint-schedule-7"),
    ("Call-Off Schedule 9 - Security", "call-off-schedule-9"),
    ("Call-Off Schedule 8  Business Continuity and Disaster Recovery",
     "call-off-schedule-8"),
    ("Call Off Schedule 15 - Call Off Contract Management", "call-off-schedule-15"),
])
def test_provided_part_names_map_onto_config_part_ids(name, expected):
    assert provided.part_id_for(name) == expected


def test_every_config_batch_part_id_is_reachable_from_a_provided_name():
    """The four batch parts must align with rows in the provided map by id."""
    reachable = {provided.part_id_for(n) for n in
                 ("Core terms", "Joint Schedule 1 - Definitions", "Framework Award Form",
                  "Call-Off Schedule 9 - Security")}
    assert {b["part"] for b in config.BATCHES.values()} <= reachable


NOTES = """# Notes

- **12 pages**, 3 constituent parts bound into one PDF.
- **An embedded outline/bookmark tree** (7 entries) is present in the file.

## Page map

| Pages | Part |
|---|---|
| 1–22 | Core terms |
| 23–30 | Framework Award Form |
| 209–209 | Joint Schedule 9 - Minimum Standards of Reliability |
"""


def test_page_map_parses_ranges_names_and_stated_counts(tmp_path):
    path = tmp_path / "DOCUMENT_NOTES.md"
    path.write_text(NOTES)
    page_map = provided.load_page_map([tmp_path / "missing.md", path])

    assert page_map.state == "loaded"
    assert page_map.source_file == str(path), "the report must cite where it read this"
    assert [r.pages for r in page_map.rows] == [(1, 22), (23, 30), (209, 209)]
    assert [r.part_id for r in page_map.rows] == ["core-terms", "award-form",
                                                  "joint-schedule-9"]
    assert page_map.stated_part_count == 3
    assert page_map.stated_page_count == 12
    assert page_map.stated_outline_entries == 7


def test_a_single_page_row_is_a_one_page_range(tmp_path):
    (tmp_path / "n.md").write_text("| Pages | Part |\n|---|---|\n| 5 | Thing |\n")
    page_map = provided.load_page_map([tmp_path / "n.md"])
    assert page_map.rows[0].pages == (5, 5)


def test_no_page_map_anywhere_is_reported_not_guessed(tmp_path):
    (tmp_path / "a.md").write_text("# nothing here\n")
    page_map = provided.load_page_map([tmp_path / "a.md"])
    assert page_map.state == "absent"
    assert "no markdown table" in page_map.error
    assert page_map.searched == [str(tmp_path / "a.md")]


def test_outline_titles_split_into_label_and_title():
    assert provided.split_label("3. What needs to be delivered") == \
        ("3", "What needs to be delivered")
    assert provided.split_label("10.4.1 Something") == ("10.4.1", "Something")
    assert provided.split_label("PART 1B: COTS Software") == ("PART 1B", "COTS Software")
    assert provided.split_label("Call-Off Schedule 25 (Supplier Operational Terms)") == \
        (None, "Call-Off Schedule 25 (Supplier Operational Terms)")


# ------------------------------------------- the real assignment, when present

needs_assignment = pytest.mark.skipif(
    not config.PDF.exists(), reason="assignment document not present")


@needs_assignment
def test_the_real_page_map_is_found_in_the_notes_and_has_48_rows():
    page_map = provided.load_page_map()
    assert page_map.state == "loaded"
    assert page_map.source_file.endswith("document/DOCUMENT_NOTES.md")
    assert len(page_map.rows) == 48
    assert page_map.stated_part_count == 46, "the notes' prose says 46 parts"
    assert page_map.stated_page_count == 475
    assert page_map.rows[0].pages == (1, 22)
    assert page_map.rows[0].part_id == "core-terms"


@needs_assignment
def test_the_embedded_outline_has_498_entries_and_48_at_the_top_level():
    outline = provided.load_outline()
    assert outline.state == "loaded"
    assert len(outline.entries) == 498, "the notes claim 498; this is the check"
    assert outline.page_count == 475
    assert len(outline.level1()) == 48
    core = outline.in_pages(1, 22)
    assert ("3", "What needs to be delivered") in \
        [(e.label, e.stripped_title) for e in core]


@needs_assignment
def test_the_two_provided_artifacts_agree_with_each_other_on_48_and_not_on_46():
    """The concrete question EVALUATION.md layer 2 asks, answered from the
    artifacts alone: the table and the outline both say 48, the prose says 46."""
    page_map = provided.load_page_map()
    outline = provided.load_outline()
    assert len(page_map.rows) == len(outline.level1()) == 48
    assert page_map.stated_part_count == 46


def test_a_missing_pdf_is_reported_rather_than_raising(tmp_path):
    outline = provided.load_outline(tmp_path / "nope.pdf")
    assert outline.state == "absent"
    assert outline.error == "PDF not found at config.PDF"
    assert outline.entries == []
