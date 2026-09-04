"""Stage 0: profiling, rulebook assignment and the five fit checks.

The fit evaluation is tested against constructed inputs so each alarm can be
made to fire and made to stay quiet, and the PDF-backed tests cover the parts
of the profile that are claims about this document.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from pipeline.parse.document import scan
from pipeline.parse.numbering import Rulebook
from pipeline.profile import (
    assign_rulebook,
    evaluate_fit,
    find_interpretation,
    profile_pages,
)

THRESHOLDS = config.QUARANTINE_THRESHOLDS


@pytest.fixture(scope="module")
def small_scan():
    return scan(config.PDF, config.BATCHES, page_range=(1, 30))


@pytest.fixture(scope="module")
def js1_scan():
    return scan(config.PDF, config.BATCHES, page_range=(112, 115))


def _clean():
    return {
        "interpretation": {
            "found": True, "cue_hits": 3, "examples": [],
            "units_named_by_document": {"Clause": [1]}, "units_unknown_to_rulebook": [],
        },
        "numbering": {"numbered_lines": 100, "unmatched_lines": 2, "rate": 0.02, "examples": []},
        "probe": {
            "orphan": {"body_ink": 1000, "residual_ink": 10, "rate": 0.01,
                       "signed_rate": 0.01, "by_part": {}, "ink_by_part": {}, "examples": []},
            "depth": {
                "max_dotted_depth": 3, "rulebook_max_dotted_depth": 3,
                "max_numbered_depth": 4, "rulebook_levels": 4, "examples": [],
            },
            "geometry": {
                "pairs_tested": 100, "disagreements": 1, "rate": 0.01,
                "parts_measured": [], "parts_abstained": [], "examples": [],
            },
        },
    }


def _fit(**overrides):
    data = _clean()
    for key, patch in overrides.items():
        if key in ("orphan", "depth", "geometry"):
            data["probe"][key].update(patch)
        else:
            data[key].update(patch)
    return evaluate_fit(data["interpretation"], data["numbering"], data["probe"], THRESHOLDS)


def test_a_document_that_fits_raises_no_alarm():
    fit = _fit()
    assert fit["passed"] is True
    assert fit["alarms"] == []


def test_check_one_fires_without_an_interpretation_clause():
    fit = _fit(interpretation={"found": False})
    assert [a["check"] for a in fit["alarms"]] == ["interpretation_clause_missing"]


def test_check_one_fires_on_a_unit_the_rulebook_does_not_know():
    fit = _fit(interpretation={"units_unknown_to_rulebook": ["Article", "Recital"]})
    assert [a["check"] for a in fit["alarms"]] == ["interpretation_names_unknown_units"]
    assert "Article" in fit["alarms"][0]["detail"]


def test_check_two_fires_above_the_configured_rate():
    limit = THRESHOLDS["max_unmatched_numbering_rate"]
    assert _fit(numbering={"rate": limit})["passed"] is True
    fit = _fit(numbering={"rate": limit + 0.01, "unmatched_lines": 30})
    assert [a["check"] for a in fit["alarms"]] == ["unmatched_numbering"]


def test_check_three_fires_on_too_much_homeless_text():
    """The residual is signed and the rate is its magnitude: a surplus means ink
    was counted twice and is as suspicious as a deficit."""
    limit = THRESHOLDS["max_orphan_block_rate"]
    assert _fit(orphan={"rate": limit})["passed"] is True
    deficit = _fit(orphan={"rate": limit + 0.01, "residual_ink": 500})
    assert [a["check"] for a in deficit["alarms"]] == ["orphan_text"]
    surplus = _fit(orphan={"rate": limit + 0.01, "residual_ink": -500})
    assert [a["check"] for a in surplus["alarms"]] == ["orphan_text"]


def test_check_four_fires_when_numbering_runs_deeper_than_the_rulebook():
    fit = _fit(depth={"max_dotted_depth": 4})
    assert [a["check"] for a in fit["alarms"]] == ["depth_out_of_range"]
    assert "4 dotted levels" in fit["alarms"][0]["detail"]


def test_check_five_fires_above_the_configured_rate():
    limit = THRESHOLDS["max_geometry_disagreement"]
    assert _fit(geometry={"rate": limit})["passed"] is True
    fit = _fit(geometry={"rate": limit + 0.01, "disagreements": 40})
    assert [a["check"] for a in fit["alarms"]] == ["geometry_disagrees_with_numbering"]


def test_check_five_stays_quiet_when_it_has_nothing_to_measure():
    """Abstention is not a pass. With no pair tested the check reports that it
    abstained rather than reporting agreement it never measured."""
    fit = _fit(geometry={"pairs_tested": 0, "rate": 0.0, "disagreements": 0,
                         "parts_abstained": [{"part": "core-terms", "reason": "no level pair separated"}]})
    assert fit["passed"] is True
    assert fit["geometry"]["pairs_tested"] == 0
    assert fit["geometry"]["parts_abstained"]


def test_every_alarm_carries_its_evidence():
    fit = _fit(numbering={"rate": 0.5, "unmatched_lines": 50,
                          "examples": [{"page": 31, "text": "1.1."}]})
    alarm = fit["alarms"][0]
    assert alarm["examples"] == [{"page": 31, "text": "1.1."}]
    assert "31" not in alarm["detail"]      # the page numbers live in the examples


def test_rulebook_is_scored_rather_than_assumed(small_scan):
    assigned, scores = assign_rulebook(small_scan, config.HIERARCHY_PROFILES)
    assert assigned in config.HIERARCHY_PROFILES
    assert assigned == config.DEFAULT_PROFILE
    assert scores[assigned]["lines_matching_grammar"] > 0
    assert 0.0 <= scores[assigned]["coverage"] <= 1.0


def test_the_interpretation_clause_is_found_and_names_the_documents_units(js1_scan):
    rulebook = Rulebook(config.DEFAULT_PROFILE, config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE])
    found = find_interpretation(js1_scan, rulebook)
    assert found["found"] is True
    named = found["units_named_by_document"]
    assert {"Clause", "Schedule", "Paragraph"} <= set(named)
    assert found["units_unknown_to_rulebook"] == []


def test_per_page_profile_reports_the_text_layer_and_font_sizes(small_scan):
    pages = profile_pages(small_scan)
    assert len(pages) == 30
    page3 = next(p for p in pages if p["page"] == 3)
    assert page3["has_text_layer"] is True
    assert page3["route"] == "text_layer"
    assert page3["printed_page"] == "3"
    assert page3["body_chars"] > 0
    assert "12.0" in page3["font_sizes"] and "18.0" in page3["font_sizes"]


def test_the_outline_is_a_flag_and_never_its_contents(small_scan):
    """The embedded 498-entry outline is a stage 8 cross-check input. Stage 0
    records that one exists and nothing about what is in it."""
    assert small_scan.has_outline is True
    assert isinstance(small_scan.has_outline, bool)
    assert not hasattr(small_scan, "outline")
    assert not hasattr(small_scan, "toc")
