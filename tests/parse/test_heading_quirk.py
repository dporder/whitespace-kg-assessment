"""The non-conforming top-level heading.

SPEC section 4 says: "The brief says Core Terms holds clauses 1 to 35, a
straightforward heading regex finds 34, so one top level heading does not match
the obvious pattern. Find it, record what made it different in `anomalies`."

Two things came out of looking, and both are pinned here because they are
measurements rather than readings of the spec.

First, in Core Terms all 35 top-level headings are typographically identical
and every straightforward heading regex finds all 35, including the one config
ships. The 34 does not reproduce there.

Second, the heading that does not match the obvious pattern is real, and it is
in Framework Schedule 5. That schedule prints eight top-level headings, and its
second is printed "2   Reporting period" with no period after the number, so
the rulebook's pattern finds seven of eight. That is the "number detached from
its period" case, and it is exactly the N versus N-1 shape the spec describes,
one part over.

The parser reads it correctly by recovering it from the part's own heading
typography and its position in the sequence, and records what made it different
on the node.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from pipeline.parse.blocks import build_blocks, collect_page
from pipeline.parse.numbering import HEADING_RECOVERY_ANOMALY

CONFIG_HEADING_RE = re.compile(config.HIERARCHY_PROFILES["uk-ccs-framework"]["numbering"]["heading"])
NAIVE_PATTERNS = {
    "config": config.HIERARCHY_PROFILES["uk-ccs-framework"]["numbering"]["heading"],
    "dot_then_capital": r"^\s{0,4}(\d{1,2})\.\s+[A-Z]",
    "dot_then_word": r"^\s{0,4}(\d{1,2})\.\s\w",
    "one_space": r"^\s{0,4}(\d{1,2})\. [A-Z]",
}


def _blocks_for(scan, part_id):
    part = scan.part_by_id(part_id)
    assert part is not None, f"{part_id} not derived from this page range"
    doc = pymupdf.open(config.PDF)
    inputs = [
        collect_page(doc[p - 1], p, scan.pages[p].furniture.body)
        for p in range(part.page_start, part.page_end + 1)
    ]
    from pipeline.parse.numbering import Rulebook

    rb = Rulebook(config.DEFAULT_PROFILE, config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE])
    blocks = build_blocks(inputs, rb)
    doc.close()
    return blocks


def test_core_terms_has_all_thirty_five_top_level_headings(core_terms_scan):
    blocks = _blocks_for(core_terms_scan, "core-terms")
    headings = [b for b in blocks if b.block_kind == "numbered" and b.depth == 1]
    numbers = [int(b.number) for b in headings]
    assert numbers == list(range(1, 36)), numbers
    assert len(headings) == 35


@pytest.mark.parametrize("name,pattern", sorted(NAIVE_PATTERNS.items()))
def test_no_naive_regex_finds_only_34_in_core_terms(core_terms_scan, name, pattern):
    """The spec's 34 does not reproduce in Core Terms, and this records the
    patterns that were tried rather than asserting the spec's number."""
    regex = re.compile(pattern)
    part = core_terms_scan.part_by_id("core-terms")
    found = set()
    for page_no in range(part.page_start, part.page_end + 1):
        for line in core_terms_scan.pages[page_no].furniture.body:
            m = regex.match(" " + line.text)
            if m and line.size_max >= 16:
                found.add(int(m.group(1)))
    assert found == set(range(1, 36)), (name, sorted(found))


def test_framework_schedule_5_heading_two_lost_its_period(fs5_scan):
    """The real non-conforming heading: eight headings, the rulebook finds seven."""
    part = fs5_scan.part_by_id("framework-schedule-5")
    assert part is not None
    strict_hits = []
    printed = []
    for page_no in range(part.page_start, part.page_end + 1):
        for line in fs5_scan.pages[page_no].furniture.body:
            if not (line.bold and line.bbox[0] < 100.0):
                continue
            loose = re.match(r"^\s*(\d{1,2})[.\s]\s*(?=[A-Z])", " " + line.text)
            if not loose:
                continue
            printed.append((int(loose.group(1)), line.text.strip()))
            if CONFIG_HEADING_RE.match(" " + line.text):
                strict_hits.append(int(loose.group(1)))

    assert [n for n, _ in printed] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert strict_hits == [1, 3, 4, 5, 6, 7, 8]
    assert len(strict_hits) == len(printed) - 1

    missed = [text for n, text in printed if n not in strict_hits]
    assert missed == ["2   Reporting period"]


def test_the_missed_heading_is_parsed_and_carries_its_anomaly(fs5_scan):
    blocks = _blocks_for(fs5_scan, "framework-schedule-5")
    headings = [b for b in blocks if b.block_kind == "numbered" and b.depth == 1]
    assert [b.number for b in headings] == ["1", "2", "3", "4", "5", "6", "7", "8"]

    recovered = [b for b in headings if any(HEADING_RECOVERY_ANOMALY in a for a in b.anomalies)]
    assert len(recovered) == 1
    block = recovered[0]
    assert block.number == "2"
    assert block.text == "Reporting period"
    note = next(a for a in block.anomalies if a.startswith(HEADING_RECOVERY_ANOMALY))
    assert "requires a period after the number" in note
    assert "heading typography and sequence position" in note
