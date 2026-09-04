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


def test_the_indent_sentinel_is_a_no_op_for_the_shipped_patterns():
    """The rulebook's patterns allow leading indentation but none of them
    requires it, so the sentinel changes no verdict today. It stays because a
    rulebook is allowed to anchor on `^\\s+`, as this one did before the
    grammar was corrected, and a PDF text layer holds indentation as geometry
    rather than as characters: every line arrives flush left."""
    for pattern in config.HIERARCHY_PROFILES["uk-ccs-framework"]["numbering"].values():
        raw = re.compile(pattern)
        for line in (
            "3. What needs to be delivered",
            "3.1 All deliverables",
            "3.1.1 The Supplier must provide Deliverables:",
            "(a) that comply with the Specification",
            "a) comply with the principles of security",
        ):
            assert bool(raw.match(line)) == bool(raw.match(INDENT_SENTINEL + line)), (
                pattern, line
            )


def test_both_printed_forms_of_a_lettered_item_are_matched(rulebook):
    """Core Terms prints "(a)", most schedules print "a)". Both are the same
    unit and both match; the key they are stored under is the bracketed form,
    and the printed form travels with the block."""
    bracketed = rulebook.match("(a) comply with the principles of security")
    bare = rulebook.match("a) comply with the principles of security")
    assert bracketed is not None and bare is not None
    assert bracketed.level == bare.level == "item"
    assert bracketed.key == bare.key == "a"
    assert bracketed.label == bare.label == "(a)"
    assert bracketed.token == "(a)" and bare.token == "a)"


def test_dotted_numbers_may_carry_a_trailing_period(rulebook):
    """Framework Schedule 1 numbers its paragraphs "1.1." and "1.1.1."."""
    clause = rulebook.match("1.1. Trailing period style")
    assert clause is not None and clause.level == "clause" and clause.key == "1.1"
    sub = rulebook.match("1.1.1. Deeper trailing period style")
    assert sub is not None and sub.level == "subclause" and sub.key == "1.1.1"


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
