"""Stages 0 to 2 are pure functions of the PDF bytes and config.

Same input, same output, byte for byte. The risks this guards against are the
ones that would not show up in a single run: a set iterated in hash order, a
dict written in insertion order that varies, a float formatted differently, or
a clock reaching a file.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from pipeline.assemble.tree import build_part, renumber
from pipeline.parse.document import scan
from pipeline.parse.layout import build_layout
from pipeline.parse.model import dump_json
from pipeline.parse.numbering import Rulebook
from pipeline.schemas import Node

PAGES = (1, 22)
PART = "core-terms"

# Anything that looks like a date or a clock inside stage output would break
# reruns, so the serialised output is checked for both.
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}|\d{2}:\d{2}:\d{2}")


def _layout_json() -> str:
    document = scan(config.PDF, config.BATCHES, page_range=PAGES)
    rulebook = Rulebook(config.DEFAULT_PROFILE, config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE])
    part = document.part_by_id(PART)
    layout = build_layout(config.PDF, document, part, rulebook, config.DOCUMENT_ID, config.BATCHES)
    return dump_json(layout.as_json())


def _tree_json(layout_text: str) -> str:
    layout = json.loads(layout_text)
    root, _ = build_part(layout, config.HIERARCHY_PROFILES[config.DEFAULT_PROFILE])
    renumber(root)
    validated = Node.model_validate(root.model_dump())
    return dump_json(validated.model_dump(mode="json", exclude_none=True))


def test_stage_one_is_byte_identical_across_runs():
    first = _layout_json()
    second = _layout_json()
    assert first == second


def test_stage_two_is_byte_identical_across_runs():
    layout = _layout_json()
    assert _tree_json(layout) == _tree_json(layout)


def test_no_timestamp_reaches_stage_output():
    layout = _layout_json()
    assert not _TIMESTAMP.search(layout)
    assert not _TIMESTAMP.search(_tree_json(layout))


def test_floats_are_rounded_once_at_the_boundary():
    """Boxes are written rounded, so no float repr difference can travel."""
    data = json.loads(_layout_json())
    for block in data["blocks"]:
        for box in block["bboxes"]:
            for value in box["bbox"]:
                assert round(value, 2) == value
