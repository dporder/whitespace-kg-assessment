"""The rulebook grammar, and the two places config.py's patterns miss the ink.

These tests pin measured facts about `config.HIERARCHY_PROFILES`, not opinions
about it. Where a pattern does not match what the page prints, the test says so
with the count, so a config change is an intentional act with evidence behind
it rather than a quiet edit.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from pipeline.parse.numbering import INDENT_SENTINEL, Rulebook, recover_heading


def test_levels_come_from_the_rulebook(rulebook: Rulebook):
    assert rulebook.levels == ["part", "heading", "clause", "subclause", "item"]
    assert rulebook.numbered_levels == ["heading", "clause", "subclause", "item"]
    assert rulebook.depth_of("heading") == 1
    assert rulebook.depth_of("item") == 4


@pytest.mark.parametrize(
    "text,level,label,key,rest",
    [
        ("3. What needs to be delivered", "heading", "3", "3", "What needs to be delivered"),
        ("3.1 All deliverables", "clause", "3.1", "3.1", "All deliverables"),
        ("3.1.1 The Supplier must provide Deliverables:", "subclause", "3.1.1", "3.1.1",
         "The Supplier must provide Deliverables:"),
        ("(a) that comply with the Specification", "item", "(a)", "a",
         "that comply with the Specification"),
        ("(iv) a roman item", "item", "(iv)", "iv", "a roman item"),
    ],
)
def test_grammar_matches_each_level(rulebook, text, level, label, key, rest):
    m = rulebook.match(text)
    assert m is not None, text
    assert (m.level, m.label, m.key) == (level, label, key)
    assert text[m.rest_start:] == rest


def test_deepest_level_wins(rulebook):
    """"3.1.2" is a subclause, not the clause "3.1" with a stray suffix."""
    assert rulebook.match("3.1.2 Something").level == "subclause"


def test_four_dotted_levels_match_nothing(rulebook):
    """The rulebook caps dotted numbering at three levels and the pack has no
    four-level numbers, so a four-level number is an unmatched anomaly rather
    than a silently accepted fifth level."""
    assert rulebook.max_dotted_depth == 3
    assert rulebook.match("3.1.2.4 Something") is None


def test_config_item_pattern_needs_the_indent_sentinel():
    """config's item pattern is anchored `^\\s+\\(`, requiring leading
    whitespace, and a PDF text layer holds indentation as geometry rather than
    as characters, so every line arrives flush left. Without the sentinel the
    pattern matches nothing at all."""
    raw = re.compile(config.HIERARCHY_PROFILES["uk-ccs-framework"]["numbering"]["item"])
    line = "(a) that comply with the Specification"
    assert raw.match(line) is None
    assert raw.match(INDENT_SENTINEL + line) is not None


def test_config_item_pattern_misses_the_bare_letter_form(rulebook):
    """Most schedules print their lettered items as "a)" with no opening
    bracket, and config's item pattern requires one. Recorded here so the gap
    is a decision rather than a surprise; see the parser-builder report for the
    measured document-wide effect."""
    assert rulebook.match("(a) comply with the principles of security") is not None
    assert rulebook.match("a) comply with the principles of security") is None


def test_bare_integers_do_not_count_as_numbering(rulebook):
    """"15 Working Days of the notification" is prose that happens to start
    with a number. Counting it as numbering the grammar failed to cover would
    measure line wrapping rather than the rulebook."""
    assert not rulebook.looks_numbered("15 Working Days of the notification from the Supplier")
    assert rulebook.looks_numbered("3.1.1 The Supplier must provide Deliverables:")
    assert rulebook.looks_numbered("1.1. Trailing period style")


def test_heading_recovery_needs_style_and_sequence(rulebook):
    text = "2   Reporting period"
    assert rulebook.match(text) is None
    assert recover_heading(text, rulebook, expected_number=2, style_matches=True) is not None
    # Wrong place in the sequence, or wrong typography, and it stays unmatched.
    assert recover_heading(text, rulebook, expected_number=5, style_matches=True) is None
    assert recover_heading(text, rulebook, expected_number=2, style_matches=False) is None


def test_recovered_heading_carries_its_anomaly(rulebook):
    m = recover_heading("2   Reporting period", rulebook, expected_number=2, style_matches=True)
    assert m is not None
    assert m.recovered is True
    assert m.label == "2"
    assert m.anomaly and m.anomaly.startswith("heading_number_missing_period")
