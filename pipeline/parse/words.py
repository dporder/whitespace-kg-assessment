"""PyMuPDF text-layer extraction: words, source lines, and visual lines.

Two structures come off every page and they answer different questions.

`SourceLine` is a line exactly as the text layer emitted it, with its own text
preserved character for character. That is what fidelity is measured against.

`VisualLine` is one row of ink as a reader sees it, built by merging source
lines whose vertical extents overlap. This matters because the pack renders the
same construct two ways: on page 3 the number and its sentence arrive as one
line ("3.1.1 The Supplier must provide Deliverables:"), while on page 2 the
number arrives as its own line at x=27.0 with the sentence beside it at x=55.4.
Merging on vertical overlap makes both look the same to everything downstream.

Word boxes come from the same page and are used only to locate the numbering
token's own box, which the spec requires stored beside the number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pymupdf

from .geometry import Box, LINE_MERGE_OVERLAP, union, vertical_overlap_ratio
from .model import SourceLine

# TEXTFLAGS_TEXT drops images and preserves ligatures/whitespace as the text
# layer holds them. No dehyphenation, no normalisation: reflow decisions are
# made explicitly in blocks.py and recorded as anomalies where they alter ink.
_FLAGS = pymupdf.TEXTFLAGS_TEXT


@dataclass(frozen=True)
class Word:
    bbox: Box
    text: str


@dataclass
class VisualLine:
    page: int
    bbox: Box
    pieces: list[SourceLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Source pieces joined left to right with exactly one space.

        Each piece keeps its own internal spacing verbatim; only the join
        between pieces is normalised, because the gap between "2.1" at x=27.0
        and its sentence at x=55.4 is horizontal whitespace with no character
        of its own in the text layer.
        """
        return " ".join(p.text.strip() for p in self.pieces if p.text.strip())

    @property
    def left(self) -> float:
        return self.bbox[0]

    @property
    def size_max(self) -> float:
        return max((p.size_max for p in self.pieces), default=0.0)

    @property
    def bold(self) -> bool:
        pieces = [p for p in self.pieces if p.text.strip()]
        return bool(pieces) and all(p.bold for p in pieces)


def page_words(page: pymupdf.Page) -> list[Word]:
    out = [
        Word(bbox=(w[0], w[1], w[2], w[3]), text=w[4])
        for w in page.get_text("words", flags=_FLAGS)
        if w[4].strip()
    ]
    out.sort(key=lambda w: (round(w.bbox[1], 2), round(w.bbox[0], 2), w.text))
    return out


def page_source_lines(page: pymupdf.Page, page_number: int) -> list[SourceLine]:
    """Every non-blank line the text layer holds, in geometric reading order."""
    data = page.get_text("dict", flags=_FLAGS)
    lines: list[SourceLine] = []
    for block in data["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            spans = line["spans"]
            text = "".join(s["text"] for s in spans)
            if not text.strip():
                continue
            inked = [s for s in spans if s["text"].strip()]
            lines.append(
                SourceLine(
                    page=page_number,
                    bbox=tuple(line["bbox"]),
                    text=text,
                    size_max=max((s["size"] for s in inked), default=0.0),
                    bold=bool(inked) and all("Bold" in s["font"] for s in inked),
                )
            )
    lines.sort(key=lambda l: (round(l.bbox[1], 2), round(l.bbox[0], 2), l.text))
    return lines


def merge_visual_lines(lines: list[SourceLine], page_number: int) -> list[VisualLine]:
    """Group source lines into visual rows by vertical overlap.

    Greedy over lines already ordered by top edge: a line joins the open row
    when it overlaps it vertically by more than LINE_MERGE_OVERLAP of the
    shorter height, otherwise it opens a new row. Deterministic given the sort.
    """
    rows: list[list[SourceLine]] = []
    for line in lines:
        placed = False
        if rows:
            current = rows[-1]
            current_box = union(p.bbox for p in current)
            if current_box and vertical_overlap_ratio(current_box, line.bbox) > LINE_MERGE_OVERLAP:
                current.append(line)
                placed = True
        if not placed:
            rows.append([line])
    out: list[VisualLine] = []
    for row in rows:
        row.sort(key=lambda l: (round(l.bbox[0], 2), round(l.bbox[1], 2)))
        box = union(p.bbox for p in row)
        assert box is not None
        out.append(VisualLine(page=page_number, bbox=box, pieces=row))
    out.sort(key=lambda r: (round(r.bbox[1], 2), round(r.bbox[0], 2)))
    return out


def word_box_for_token(words: list[Word], line: VisualLine, token: str) -> Optional[Box]:
    """Box of the leftmost word on this line whose text is `token`.

    Used for the numbering token's own box. Numbering tokens in this family are
    always a single whitespace-delimited word ("3.1.2", "(a)", "35."), so one
    word is enough; a token that is not found returns None rather than guessing.
    """
    candidates = [
        w for w in words
        if vertical_overlap_ratio(w.bbox, line.bbox) > LINE_MERGE_OVERLAP and w.text == token
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda w: (round(w.bbox[0], 2), round(w.bbox[1], 2))).bbox


def font_size_histogram(lines: list[SourceLine]) -> dict[str, int]:
    """Rounded font size -> count of source lines whose largest span is that size."""
    hist: dict[str, int] = {}
    for line in lines:
        key = f"{line.size_max:.1f}"
        hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: float(kv[0])))
