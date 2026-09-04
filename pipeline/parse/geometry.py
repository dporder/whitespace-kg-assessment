"""Geometry primitives and the tolerances stages 1 and 2 measure against.

Every tunable value comes from `config.PARSE_GEOMETRY`, so no threshold is
buried in code. The measurement that justifies each one stays here beside the
import, because the number is only defensible with the observation attached.
Coordinates are PyMuPDF points with the origin at the top-left of the page, so
smaller y is higher up the page.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config

Box = tuple[float, float, float, float]   # x0, y0, x1, y1

_GEOMETRY = config.PARSE_GEOMETRY

# Glyph origins for the same logical indent differ by a fraction of a point
# between fonts: in Core Terms clause "3.1" starts at x=27.0 and its child
# "3.1.1" at x=26.4 for identical logical indentation. The configured 2.0pt
# covers the observed jitter without hiding a real indent step (the smallest
# real step in this document is 3.4pt, heading to clause in Core Terms).
INDENT_TOLERANCE = _GEOMETRY["indent_tolerance"]

# A part is treated as carrying usable indentation only when consecutive
# numbering depths are separated by at least this much. Core Terms separates
# its dotted depths by under 4pt in total (30.4 / 27.0 / 26.4), so indentation
# there carries no depth signal and the geometry check abstains instead of
# firing on every line. Call-Off Schedule 9 separates by 72.0 / 75.8 / 117.0.
MIN_INDENT_STEP = _GEOMETRY["min_indent_step"]

# Vertical tolerance when asserting "at or above" and "siblings ascend".
VERTICAL_TOLERANCE = 1.0

# A line's box spans the font's full ascent and descent, which exceeds the
# leading between lines, so consecutive lines overlap vertically by a point or
# two purely from font metrics: measured at 0.8pt for 12pt body text and 2.9pt
# for 16pt. Sibling overlap is therefore measured as a share of line height. A
# genuinely out-of-order sibling overlaps by most of a line or starts above the
# previous one entirely, which the reading-order check catches separately.
SIBLING_OVERLAP_SHARE = _GEOMETRY["sibling_overlap_share"]


def sibling_overlap_tolerance(*boxes: Box) -> float:
    heights = [b[3] - b[1] for b in boxes if b[3] > b[1]]
    if not heights:
        return VERTICAL_TOLERANCE
    return max(VERTICAL_TOLERANCE, SIBLING_OVERLAP_SHARE * min(heights))

# Two pieces of ink belong to the same visual line when their vertical extents
# overlap by more than this share of the shorter one. PyMuPDF emits the number
# "2.1" and its sentence as separate lines at y=146.9 and y=147.1 on page 2;
# they must merge. Distinct lines are ~14pt apart, so 0.5 is unambiguous.
LINE_MERGE_OVERLAP = 0.5

# Bands, as a fraction of page height, in which a line may be furniture. A line
# inside a band is furniture only if it also repeats (see parse/furniture.py):
# position gates, repetition decides. Measured: the lowest body line in the pack
# sits at y0=739.4 (0.878H) and the highest at y1=95.8 (0.114H), while the
# closest furniture is at y1=82.4 (0.098H) and y0=753.3 (0.895H).
HEADER_BAND = _GEOMETRY["header_band"]
FOOTER_BAND = _GEOMETRY["footer_band"]


def union(boxes: Iterable[Box]) -> Optional[Box]:
    boxes = list(boxes)
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def round_box(b: Box, places: int = 2) -> Box:
    """Rounded once, at the boundary, so JSON output is byte-stable."""
    return (round(b[0], places), round(b[1], places), round(b[2], places), round(b[3], places))


def vertical_overlap_ratio(a: Box, b: Box) -> float:
    """Share of the shorter box's height that the two boxes share vertically."""
    lo = max(a[1], b[1])
    hi = min(a[3], b[3])
    if hi <= lo:
        return 0.0
    shorter = min(a[3] - a[1], b[3] - b[1])
    return (hi - lo) / shorter if shorter > 0 else 0.0


def contains(outer: Box, inner: Box, tol: float = INDENT_TOLERANCE) -> bool:
    return (
        inner[0] >= outer[0] - tol
        and inner[1] >= outer[1] - tol
        and inner[2] <= outer[2] + tol
        and inner[3] <= outer[3] + tol
    )


def median(values: Sequence[float]) -> float:
    """Deterministic median: the lower of the two middles on even counts."""
    if not values:
        raise ValueError("median of empty sequence")
    s = sorted(values)
    return s[(len(s) - 1) // 2]
