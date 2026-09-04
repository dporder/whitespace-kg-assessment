"""Header and footer stripping, and what is read back out of it."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.parse.furniture import (
    MIN_REPETITIONS,
    count_repetitions,
    normalise_for_repetition,
    normalise_version,
    split_page,
)
from pipeline.parse.model import SourceLine

HEIGHT = 841.8


def _line(text, x0=72.0, y0=33.8, y1=48.2, page=1):
    return SourceLine(page=page, bbox=(x0, y0, 520.0, y1), text=text, size_max=10.0, bold=False)


def test_page_counter_normalises_to_one_repeated_form():
    assert normalise_for_repetition("22 ") == "#"
    assert normalise_for_repetition("Model Version: v3.10 ") == "Model Version: v#.#"


def test_running_furniture_is_stripped_and_body_is_not(core_terms_scan):
    page = core_terms_scan.pages[3]
    stripped = [l.text.strip() for l in page.furniture.stripped]
    body = [l.text.strip() for l in page.furniture.body]
    assert "Crown Copyright 2018." in stripped
    assert "Core Terms" in stripped
    assert "Version: 3.0.11" in stripped
    assert "3" in stripped                       # the printed page number
    assert any(t.startswith("3. What needs to be delivered") for t in body)
    assert not any("Crown Copyright" in t for t in body)


def test_a_heading_high_on_the_page_survives_position(core_terms_scan):
    """Core Terms sets clause headings 1 and 28 at y0=70.0, inside any band
    wide enough to hold its own running header. Repetition is what keeps them:
    they appear once each, the header appears on every page."""
    page2 = [l.text.strip() for l in core_terms_scan.pages[2].furniture.body]
    assert any(t.startswith("1. Definitions used in the contract") for t in page2)
    page20 = [l.text.strip() for l in core_terms_scan.pages[20].furniture.body]
    assert any(t.startswith("28. Equality, diversity and human rights") for t in page20)


def test_slot_repetition_keeps_short_parts_titled(core_terms_scan):
    """A one or two page schedule's title never repeats three times as text.
    The slot it occupies does, which is what stops a short part being folded
    into the one before it."""
    def page_lines(title, page):
        # Every page also carries the constant line that proves this page runs
        # furniture at all, which is what licenses the slot rule.
        return [
            _line(title, page=page),
            _line("Crown Copyright 2018", page=page, y0=48.5, y1=63.2),
        ]

    lines = {
        1: page_lines("Framework Schedule 2 (Framework Tender)", 1),
        2: page_lines("Framework Schedule 3 (Framework Prices)", 2),
        3: page_lines("Framework Schedule 4 (Framework Management)", 3),
        4: page_lines("Framework Schedule 5 (Management Charges)", 4),
    }
    heights = {p: HEIGHT for p in lines}
    reps = count_repetitions(lines, heights)
    assert max(reps.values()) >= MIN_REPETITIONS
    page = split_page(1, lines[1], HEIGHT, reps)
    assert page.header_title == "Framework Schedule 2 (Framework Tender)"
    assert page.body == []


def test_the_footer_is_stripped_by_words_not_by_slot():
    """Footnote continuations run along the bottom of pages 468 and 469 in the
    same slot the page counter uses on other pages. They are content, so slot
    repetition is confined to the header band."""
    footnote = SourceLine(
        page=1, bbox=(72.0, 756.9, 525.7, 770.3),
        text="contained in Section 19 and Part 7 of the Finance Act 2004",
        size_max=10.0, bold=False,
    )
    counter = SourceLine(page=1, bbox=(294.8, 778.7, 302.9, 793.4), text="8 ", size_max=10.0, bold=False)
    lines = {p: [footnote, counter] for p in (1, 2, 3, 4)}
    heights = {p: HEIGHT for p in lines}
    reps = count_repetitions(lines, heights)
    page = split_page(1, lines[1], HEIGHT, reps)
    assert page.printed_page == "8"
    # The footnote repeats as text here only because the fixture repeats it;
    # what matters is that it never reaches the header-title slot rule.
    assert page.header_title is None


def test_printed_page_and_versions_come_off_the_furniture(core_terms_scan):
    assert core_terms_scan.pages[3].furniture.printed_page == "3"
    assert core_terms_scan.pages[3].furniture.header_version_raw == "3.0.11"
    assert core_terms_scan.pages[24].furniture.printed_page == "2"
    assert core_terms_scan.pages[24].furniture.model_version_raw == "v3.10"
    assert core_terms_scan.pages[24].furniture.project_version_raw == "vFinal1.1"


def test_version_key_is_minted_not_edited():
    """Normalisation mints the key only. The printed form is stored beside it."""
    assert normalise_version("3.0.11") == ("v3.0.11", True)
    assert normalise_version("v3.10") == ("v3.10", False)
    assert normalise_version("v 3.3") == ("v3.3", True)
    assert normalise_version(None) == ("v0", True)
    assert normalise_version("  ") == ("v0", True)
