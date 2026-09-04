"""Shared PDF scans for the stage 1 tests.

Scans are session scoped and page ranged, because every one of them reopens the
same read-only PDF and a narrow range costs a tenth of a second where the whole
document costs ten seconds.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config                                    # noqa: E402
from pipeline.parse.document import scan          # noqa: E402
from pipeline.parse.numbering import Rulebook     # noqa: E402


@pytest.fixture(scope="session")
def rulebook() -> Rulebook:
    return Rulebook(config.DEFAULT_PROFILE, config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE])


@pytest.fixture(scope="session")
def core_terms_scan():
    """Core Terms and the Award Form, pages 1 to 30."""
    return scan(config.PDF, config.BATCHES, page_range=(1, 30))


@pytest.fixture(scope="session")
def definitions_scan():
    """Joint Schedule 1's two-column definitions, first pages only."""
    return scan(config.PDF, config.BATCHES, page_range=(112, 118))


@pytest.fixture(scope="session")
def fs5_scan():
    """Framework Schedules 4 to 6, which is where the heading with no period is."""
    return scan(config.PDF, config.BATCHES, page_range=(78, 86))


@pytest.fixture(scope="session")
def cos9_scan():
    """Call-Off Schedule 9, whose two parts each open with a heavy-ruled
    definitions block."""
    return scan(config.PDF, config.BATCHES, page_range=(340, 361))
