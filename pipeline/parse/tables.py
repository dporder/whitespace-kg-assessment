"""Tables and form rows, detected from the page's own ruled grid.

Column detection runs on box geometry, never on line order. That is what makes
the two-column definitions schedule parse even though its quoted terms wrap
across lines inside a cell, and across page breaks: a cell is a rectangle in
the grid, and a line belongs to whichever rectangle its centre falls in, no
matter how many lines the cell holds or which order PyMuPDF emits them.

PyMuPDF's own `find_tables()` is not used. It reports three columns for the
two-column definitions layout and nineteen for the Award Form, because it
infers structure from whitespace as well as rules. The vector rules themselves
are unambiguous, so they are read directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pymupdf

from .geometry import Box, union
from .model import Cell, SourceLine
from .words import merge_visual_lines

# A drawn rectangle counts as a rule when one side is hairline and the other is
# long enough to bound a cell. Table borders in this pack are filled rectangles
# roughly 0.5 to 1.0pt thick.
RULE_THICKNESS = 2.0
RULE_MIN_LENGTH = 5.0

# Rules drawn twice, or drawn as two abutting segments, land within a point of
# each other and mean one boundary.
SNAP = 1.5

# A grid is a table only if it actually partitions ink in two directions.
MIN_COLS = 2
MIN_ROWS = 1


@dataclass
class Grid:
    page: int
    cols: list[float]       # column boundaries, left to right, len == n_cols + 1
    rows: list[float]       # row boundaries, top to bottom, len == n_rows + 1

    @property
    def n_cols(self) -> int:
        return len(self.cols) - 1

    @property
    def n_rows(self) -> int:
        return len(self.rows) - 1

    @property
    def bbox(self) -> Box:
        return (self.cols[0], self.rows[0], self.cols[-1], self.rows[-1])

    def cell_box(self, row: int, col: int) -> Box:
        return (self.cols[col], self.rows[row], self.cols[col + 1], self.rows[row + 1])

    def locate(self, box: Box) -> Optional[tuple[int, int]]:
        """Row and column containing the centre of `box`, or None if outside."""
        cx = (box[0] + box[2]) / 2.0
        cy = (box[1] + box[3]) / 2.0
        col = _slot(self.cols, cx)
        row = _slot(self.rows, cy)
        if col is None or row is None:
            return None
        return row, col


def _slot(bounds: list[float], value: float) -> Optional[int]:
    for i in range(len(bounds) - 1):
        if bounds[i] <= value < bounds[i + 1]:
            return i
    return None


def _snap(values: list[float]) -> list[float]:
    """Collapse near-identical coordinates, keeping the smallest of each group."""
    out: list[float] = []
    for v in sorted(values):
        if out and v - out[-1] <= SNAP:
            continue
        out.append(round(v, 2))
    return out


def _intervals_overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return min(a[1], b[1]) - max(a[0], b[0]) > 0


def page_grids(page: pymupdf.Page, page_number: int) -> list[Grid]:
    """Every ruled grid on the page, top to bottom.

    Vertical rules are clustered by overlapping vertical extent so two stacked
    tables with different column sets stay two tables; horizontal rules join the
    cluster whose vertical range contains them.
    """
    verticals: list[tuple[float, float, float]] = []   # x, y0, y1
    horizontals: list[tuple[float, float, float]] = []  # y, x0, x1
    for item in page.get_drawings():
        r = item["rect"]
        if r.width <= RULE_THICKNESS and r.height >= RULE_MIN_LENGTH:
            verticals.append((r.x0, r.y0, r.y1))
        elif r.height <= RULE_THICKNESS and r.width >= RULE_MIN_LENGTH:
            horizontals.append((r.y0, r.x0, r.x1))
    if not verticals or not horizontals:
        return []

    # Borders are drawn one segment per row, so a column rule arrives as a
    # stack of abutting hairlines rather than one long line. Rejoin each
    # column's segments before clustering, or every row of the definitions
    # schedule becomes its own table.
    by_x: dict[float, list[tuple[float, float]]] = {}
    for x, y0, y1 in verticals:
        key = min(
            (k for k in by_x if abs(k - x) <= SNAP),
            key=lambda k: (abs(k - x), k),
            default=round(x, 2),
        )
        by_x.setdefault(key, []).append((y0, y1))
    spans: list[tuple[float, float, float]] = []
    for x in sorted(by_x):
        segments = sorted(by_x[x])
        current_lo, current_hi = segments[0]
        for lo, hi in segments[1:]:
            if lo - current_hi <= 2 * SNAP:
                current_hi = max(current_hi, hi)
            else:
                spans.append((x, current_lo, current_hi))
                current_lo, current_hi = lo, hi
        spans.append((x, current_lo, current_hi))

    spans.sort(key=lambda s: (round(s[1], 2), round(s[0], 2)))
    clusters: list[list[tuple[float, float, float]]] = []
    for span in spans:
        if clusters:
            cluster = clusters[-1]
            crange = (min(c[1] for c in cluster), max(c[2] for c in cluster))
            if _intervals_overlap((span[1], span[2]), crange):
                cluster.append(span)
                continue
        clusters.append([span])

    grids: list[Grid] = []
    for cluster in clusters:
        cols = _snap([v[0] for v in cluster])
        y_lo = min(v[1] for v in cluster)
        y_hi = max(v[2] for v in cluster)
        rows = _snap([
            h[0] for h in horizontals
            if y_lo - SNAP <= h[0] <= y_hi + SNAP
            and _intervals_overlap((h[1], h[2]), (cols[0], cols[-1]))
        ])
        # The cluster's own vertical extent bounds the grid even where the
        # topmost or bottommost horizontal rule was not drawn.
        if not rows or rows[0] - y_lo > SNAP:
            rows = _snap([y_lo] + rows)
        if rows[-1] < y_hi - SNAP:
            rows = _snap(rows + [y_hi])
        if len(cols) - 1 < MIN_COLS or len(rows) - 1 < MIN_ROWS:
            continue
        grids.append(Grid(page=page_number, cols=cols, rows=rows))
    grids.sort(key=lambda g: (g.rows[0], g.cols[0]))
    return grids


import re

_ROW_NUMBER = re.compile(r"^\s*(\d{1,3})\.?\s*$")
_UNPAIRED_CLOSE = re.compile(r'^[^"“”]*[”"]\s*$')


def fill_cells(
    grid: Grid, lines: list[SourceLine], words: list
) -> tuple[list[Cell], list[SourceLine]]:
    """Assign body lines to grid cells. Returns (cells, lines left outside).

    A line that crosses a column boundary is split there rather than being
    handed whole to whichever cell holds its midpoint. The Award Form needs
    this: "1. CCS" is one line running from x=36 to x=89.7 across the rule at
    x=51.8, so its row number belongs to the first column and its label to the
    second, and a midpoint rule would file the number under the label.
    """
    buckets: dict[tuple[int, int], list[SourceLine]] = {}
    outside: list[SourceLine] = []
    for line in lines:
        row = _slot(grid.rows, (line.bbox[1] + line.bbox[3]) / 2.0)
        if row is None:
            outside.append(line)
            continue
        for col, piece in _split_by_column(grid, line, words):
            buckets.setdefault((row, col), []).append(piece)

    header_row = _detect_header_row(grid, buckets)
    number_col = _detect_number_column(grid, buckets)
    cells: list[Cell] = []
    for row in range(grid.n_rows):
        for col in range(grid.n_cols):
            content = buckets.get((row, col), [])
            content.sort(key=lambda l: (round(l.bbox[1], 2), round(l.bbox[0], 2)))
            # Reading order inside a cell is by row then left to right, not by
            # top edge alone: the Award Form sets "Name:" at x=161.4, y=267.3
            # beside "[Insert name...]" at x=280.0, y=266.9, and ordering on the
            # raw top edge would read the value before its own label.
            text = " ".join(
                l.text.strip()
                for visual in merge_visual_lines(content, grid.page)
                for l in visual.pieces
                if l.text.strip()
            )
            box = union([l.bbox for l in content]) or grid.cell_box(row, col)
            role, confidence = _role_for(
                row, col, grid, text, header_row=header_row, number_col=number_col
            )
            anomalies: list[str] = []
            if text and _UNPAIRED_CLOSE.match(text):
                anomalies.append(
                    "unpaired_closing_quote_in_cell: the text layer carries the "
                    "closing quotation mark but no opening one; recorded verbatim"
                )
            cells.append(
                Cell(
                    row=row,
                    col=col,
                    text=text,
                    page=grid.page,
                    bbox=box,
                    role=role,
                    role_confidence=confidence,
                    lines=content,
                    anomalies=anomalies,
                )
            )
    return cells, outside


def _split_by_column(grid: Grid, line: SourceLine, words: list) -> list[tuple[int, SourceLine]]:
    """One (column, piece) pair per column the line puts ink in."""
    from .geometry import vertical_overlap_ratio

    own = [
        w for w in words
        if vertical_overlap_ratio(w.bbox, line.bbox) > 0.5
        and w.bbox[0] >= line.bbox[0] - SNAP
        and w.bbox[2] <= line.bbox[2] + SNAP
    ]
    grouped: dict[int, list] = {}
    for word in own:
        col = _slot(grid.cols, (word.bbox[0] + word.bbox[2]) / 2.0)
        if col is None:
            continue
        grouped.setdefault(col, []).append(word)
    if len(grouped) <= 1:
        col = _slot(grid.cols, (line.bbox[0] + line.bbox[2]) / 2.0)
        if not grouped and col is None:
            return []
        only = next(iter(grouped), col)
        return [(only, line)] if only is not None else []
    pieces: list[tuple[int, SourceLine]] = []
    for col in sorted(grouped):
        group = sorted(grouped[col], key=lambda w: (round(w.bbox[0], 2), w.text))
        box = union([w.bbox for w in group])
        assert box is not None
        pieces.append(
            (
                col,
                SourceLine(
                    page=line.page,
                    bbox=box,
                    text=" ".join(w.text for w in group),
                    size_max=line.size_max,
                    bold=line.bold,
                ),
            )
        )
    return pieces


def _detect_header_row(grid: Grid, buckets: dict[tuple[int, int], list[SourceLine]]) -> Optional[int]:
    """Row 0 is a header row when all its inked cells are bold and at least one
    later row is not. Recorded as what it physically is; how plausible that is
    lives in role_confidence."""
    if grid.n_rows < 3:
        return None
    row0 = [l for col in range(grid.n_cols) for l in buckets.get((0, col), [])]
    if not row0 or not all(l.bold for l in row0):
        return None
    for row in range(1, grid.n_rows):
        rest = [l for col in range(grid.n_cols) for l in buckets.get((row, col), [])]
        if rest and not all(l.bold for l in rest):
            return 0
    return None


def _detect_number_column(grid: Grid, buckets: dict[tuple[int, int], list[SourceLine]]) -> Optional[int]:
    """Column 0 is a row-number column when most of its inked cells hold only a
    number. That is what makes the Award Form a set of numbered form rows rather
    than a three-column table."""
    if grid.n_cols < 3:
        return None
    inked = 0
    numeric = 0
    for row in range(grid.n_rows):
        content = buckets.get((row, 0), [])
        text = " ".join(l.text.strip() for l in content if l.text.strip())
        if not text:
            continue
        inked += 1
        if _ROW_NUMBER.match(text):
            numeric += 1
    if inked and numeric * 2 >= inked:
        return 0
    return None


def _role_for(
    row: int,
    col: int,
    grid: Grid,
    text: str,
    header_row: Optional[int],
    number_col: Optional[int],
) -> tuple[str, float]:
    if header_row is not None and row == header_row:
        # Fidelity and usability kept separately: the role records what the cell
        # physically is, the confidence records how plausible that role looks.
        plausible = len(text) <= 80 and not text.rstrip().endswith(".")
        return "header", 0.9 if plausible else 0.4
    if number_col is not None:
        if col == number_col:
            return "label", 0.98
        if col == number_col + 1:
            return "label", 0.98
        return "value", 0.98
    return ("label", 0.99) if col == 0 else ("value", 0.99)


def row_number_of(cells: list[Cell], row: int, number_col: int) -> Optional[str]:
    for cell in cells:
        if cell.row == row and cell.col == number_col and cell.text.strip():
            m = _ROW_NUMBER.match(cell.text)
            if m:
                return m.group(1)
    return None


def is_form_grid(grid: Grid, cells: list[Cell]) -> bool:
    if grid.n_cols < 3:
        return False
    inked = numeric = 0
    for cell in cells:
        if cell.col != 0 or not cell.text.strip():
            continue
        inked += 1
        if _ROW_NUMBER.match(cell.text):
            numeric += 1
    return bool(inked) and numeric * 2 >= inked
