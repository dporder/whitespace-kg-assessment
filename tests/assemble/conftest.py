"""Layout fixtures for the stage 2 tests.

Stage 2 is a pure function of a layout file, so these tests build the layout
once per part and assemble from it. Nothing here reads `output/`: the tests
must pass on a clean checkout with no prior run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config                                    # noqa: E402
from pipeline.parse.document import scan          # noqa: E402
from pipeline.parse.layout import build_layout    # noqa: E402
from pipeline.parse.numbering import Rulebook     # noqa: E402


def _layout(page_range, part_id):
    document = scan(config.PDF, config.BATCHES, page_range=page_range)
    rulebook = Rulebook(config.DEFAULT_PROFILE, config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE])
    part = document.part_by_id(part_id)
    assert part is not None, f"{part_id} not derived from pages {page_range}"
    return build_layout(
        config.PDF, document, part, rulebook, config.DOCUMENT_ID, config.BATCHES
    ).as_json()


@pytest.fixture(scope="session")
def profile() -> dict:
    return config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE]


@pytest.fixture(scope="session")
def core_terms_layout() -> dict:
    return _layout((1, 22), "core-terms")


@pytest.fixture(scope="session")
def award_form_layout() -> dict:
    return _layout((23, 30), "award-form")


@pytest.fixture(scope="session")
def definitions_layout() -> dict:
    return _layout((112, 118), "joint-schedule-1")
