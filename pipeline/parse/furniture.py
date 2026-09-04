"""Header and footer stripping, and the two things the furniture carries.

Position gates, repetition decides. A line is only a furniture candidate if it
sits wholly inside the header or footer band, and it is only stripped if its
normalised form recurs across pages. That order matters here: Core Terms puts
clause headings "1." and "28." at y0=70.0, inside any band wide enough to hold
its own running header, and a position-only rule would silently eat two of the
document's 35 top-level clauses.

What survives stripping is kept, not discarded: every stripped line is written
into the layout file with its box, and two fields are read back out of it.

- The printed page number, which restarts per part and is what a lawyer quotes.
- The template version. The spec says footers carry it and for every schedule
  they do ("Model Version: v3.10"), but Core Terms carries its version in the
  running header instead ("Version: 3.0.11"). Both are read, the footer form
  wins where both exist, and which band supplied it is recorded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .geometry import FOOTER_BAND, HEADER_BAND
from .model import SourceLine

# Normalisation for the repetition count only; the stored line keeps its ink.
_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")

# A candidate must recur on at least this many pages of the document to be
# treated as running furniture. Three pages distinguishes a running header from
# a heading that happens to sit high on two pages.
MIN_REPETITIONS = 3

_PRINTED_PAGE = re.compile(r"^\s*(\d{1,4})\s*$")
_MODEL_VERSION = re.compile(r"^\s*Model Version\s*:\s*(\S.*?)\s*$")
_HEADER_VERSION = re.compile(r"^\s*Version\s*:\s*(\S.*?)\s*$")
_PROJECT_VERSION = re.compile(r"^\s*Project Version\s*:\s*(\S.*?)\s*$")


def normalise_for_repetition(text: str) -> str:
    """Digits collapse to '#' so a page counter counts as one repeated form."""
    return _DIGITS.sub("#", _WS.sub(" ", text).strip())


@dataclass
class PageFurniture:
    page: int
    stripped: list[SourceLine] = field(default_factory=list)
    body: list[SourceLine] = field(default_factory=list)
    printed_page: Optional[str] = None
    header_title: Optional[str] = None
    model_version_raw: Optional[str] = None
    header_version_raw: Optional[str] = None
    project_version_raw: Optional[str] = None


def _band(line: SourceLine, height: float) -> Optional[str]:
    if line.bbox[3] <= HEADER_BAND * height:
        return "header"
    if line.bbox[1] >= FOOTER_BAND * height:
        return "footer"
    return None


def count_repetitions(pages: dict[int, list[SourceLine]], heights: dict[int, float]) -> dict[str, int]:
    """How many distinct pages each normalised in-band line form appears on."""
    counts: dict[str, set[int]] = {}
    for page_no, lines in pages.items():
        height = heights[page_no]
        seen: set[str] = set()
        for line in lines:
            if _band(line, height) is None:
                continue
            key = normalise_for_repetition(line.text)
            if not key:
                continue
            seen.add(key)
        for key in seen:
            counts.setdefault(key, set()).add(page_no)
    return {k: len(v) for k, v in sorted(counts.items())}


def split_page(
    page_no: int,
    lines: list[SourceLine],
    height: float,
    repetitions: dict[str, int],
) -> PageFurniture:
    out = PageFurniture(page=page_no)
    for line in lines:
        band = _band(line, height)
        key = normalise_for_repetition(line.text)
        if band is not None and repetitions.get(key, 0) >= MIN_REPETITIONS:
            out.stripped.append(line)
        else:
            out.body.append(line)

    # Read the two carried fields off the stripped furniture only.
    for line in out.stripped:
        text = line.text
        m = _PRINTED_PAGE.match(text)
        if m and out.printed_page is None:
            out.printed_page = m.group(1)
            continue
        m = _MODEL_VERSION.match(text)
        if m and out.model_version_raw is None:
            out.model_version_raw = m.group(1)
            continue
        m = _PROJECT_VERSION.match(text)
        if m and out.project_version_raw is None:
            out.project_version_raw = m.group(1)
            continue
        m = _HEADER_VERSION.match(text)
        if m and out.header_version_raw is None:
            out.header_version_raw = m.group(1)
    out.header_title = _pick_title(out.stripped, height)
    return out


# Furniture lines that are boilerplate rather than the part's identity.
_BOILER = re.compile(
    r"^\s*(Crown Copyright|Version\s*:|Model Version\s*:|Project Version\s*:"
    r"|Framework Ref\s*:|Call-Off Ref\s*:|Order Ref\s*:|\d+)\s*",
    re.IGNORECASE,
)


def _pick_title(stripped: list[SourceLine], height: float) -> Optional[str]:
    """The part's own name: the first non-boilerplate furniture line, header
    band preferred, reading top to bottom then left to right."""
    for band in ("header", "footer"):
        candidates = [l for l in stripped if _band(l, height) == band]
        candidates.sort(key=lambda l: (round(l.bbox[1], 2), round(l.bbox[0], 2)))
        for line in candidates:
            text = line.text.strip()
            if not text or _BOILER.match(text):
                continue
            return text
    return None


def normalise_version(raw: Optional[str]) -> tuple[str, bool]:
    """Mint the version key used in node ids. Returns (key, changed).

    Keys only, exactly like legislation normalisation: the raw printed string is
    stored beside it and never overwritten. "3.0.11" (Core Terms header) and
    "v 3.3" (Joint Schedule 8 footer) both name a version whose key is v-prefixed
    and space-free; a part whose furniture names no version gets "v0" and an
    anomaly from the caller.
    """
    if raw is None:
        return "v0", True
    key = _WS.sub("", raw.strip())
    if not key:
        return "v0", True
    if not key.lower().startswith("v"):
        key = "v" + key
    key = "v" + key[1:]
    return key, key != raw.strip()
