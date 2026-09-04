"""Part boundary detection, derived from the PDF alone."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from pipeline.parse.parts import canonicalise_ids, detect_parts, family_for, slugify


def test_slug_drops_the_parenthetical_subject():
    assert slugify("Core Terms") == "core-terms"
    assert slugify("Joint Schedule 1 (Definitions)") == "joint-schedule-1"
    assert slugify("Call-Off Schedule 9 (Security)") == "call-off-schedule-9"
    assert slugify("Call-Off Schedule 13: (Implementation Plan and Testing)") == "call-off-schedule-13"


def test_family_comes_from_the_title():
    assert family_for("Core Terms") == "core"
    assert family_for("Framework Award Form") == "award-form"
    assert family_for("Framework Schedule 4 (Framework Management)") == "framework-schedule"
    assert family_for("Joint Schedule 11 (Processing Data)") == "joint-schedule"
    assert family_for("Call-Off Schedule 9 (Security)") == "call-off-schedule"
    assert family_for("Something Else Entirely") is None


def test_a_page_with_no_title_continues_the_part_before_it():
    signatures = [
        (1, "Call-Off Schedule 22 (Lease Terms)", "v1.1", None, None),
        (2, None, None, None, None),
        (3, None, None, None, None),
        (4, "RM3808 Call-Off Schedule 22 (Supplier-Furnished Terms)", "v3.1", None, None),
    ]
    parts = detect_parts(signatures)
    assert [(p.slug, p.page_start, p.page_end) for p in parts] == [
        ("call-off-schedule-22", 1, 3),
        ("rm3808-call-off-schedule-22", 4, 4),
    ]


def test_a_version_change_under_one_title_is_an_anomaly_not_a_boundary():
    """Joint Schedule 2 prints v3.1, v3.0 and v3.1 on its three pages. That is a
    versioning inconsistency in the pack, not three parts."""
    signatures = [
        (140, "Joint Schedule 2 (Variation Form)", "v3.1", None, None),
        (141, "Joint Schedule 2 (Variation Form)", "v3.0", None, None),
        (142, "Joint Schedule 2 (Variation Form)", "v3.1", None, None),
    ]
    parts = detect_parts(signatures)
    assert len(parts) == 1
    assert (parts[0].page_start, parts[0].page_end) == (140, 142)
    assert any(a.startswith("template_version_varies_within_part") for a in parts[0].anomalies)


def test_duplicate_titles_get_distinct_ids():
    signatures = [
        (1, "Annex", None, None, None),
        (2, "Something", None, None, None),
        (3, "Annex", None, None, None),
    ]
    parts = detect_parts(signatures)
    assert [p.slug for p in parts] == ["annex", "something", "annex-2"]
    assert any(a.startswith("duplicate_part_title") for a in parts[2].anomalies)


def test_config_batch_ids_win_over_the_derived_slug():
    signatures = [(p, "Framework Award Form", "v3.10", None, None) for p in range(23, 31)]
    parts = detect_parts(signatures)
    assert parts[0].slug == "framework-award-form"
    renames = canonicalise_ids(parts, config.BATCHES)
    assert renames == {"framework-award-form": "award-form"}
    assert parts[0].slug == "award-form"
    assert any(a.startswith("part_id_canonicalised") for a in parts[0].anomalies)


def test_derived_parts_match_the_batch_definitions(core_terms_scan):
    ids = [p.slug for p in core_terms_scan.parts]
    assert ids == ["core-terms", "award-form"]
    core = core_terms_scan.part_by_id("core-terms")
    award = core_terms_scan.part_by_id("award-form")
    assert (core.page_start, core.page_end) == config.BATCHES["B1"]["pages"]
    assert (award.page_start, award.page_end) == config.BATCHES["B3"]["pages"]
