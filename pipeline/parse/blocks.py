"""Visual lines and ruled grids to blocks, in reading order.

A block is one unit of ink: a numbered provision with its wrapped lines, a run
of unnumbered prose, the part's cover title, or a table with its cells. Stage 2
turns blocks into the provision tree; nothing here decides parentage, kind or
level semantics beyond the numbering depth the rulebook assigns.

Reflow is the one place ink changes shape, so it is done explicitly. Wrapped
lines join with a single space and every source line survives on the block, so
a consumer can always see what the layer actually held. Where a line ends on a
hyphen and the next begins lower case, the join inserts a space the page does
not contain; that is recorded as an anomaly rather than repaired, because
de-hyphenating "subject-\\nmatter" into "subject-matter" and de-hyphenating
"Call-\\nOff" into "CallOff" are the same edit with different outcomes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import pymupdf

from .geometry import INDENT_TOLERANCE, union
from .model import Block, Cell, PageBox, SourceLine
from .numbering import Rulebook, recover_heading
from .tables import Grid, fill_cells, is_form_grid, page_grids
from .words import VisualLine, merge_visual_lines, page_words, word_box_for_token

# A cover title is set far larger than the body it introduces.
COVER_TITLE_RATIO = 1.4

_HYPHEN_BREAK = re.compile(r"-$")
_LOWER_START = re.compile(r"^[a-z]")


@dataclass
class PageInput:
    page: int
    body: list[SourceLine]
    grids: list[Grid]
    words: list
    height: float
    width: float


def collect_page(page: pymupdf.Page, page_number: int, body: list[SourceLine]) -> PageInput:
    return PageInput(
        page=page_number,
        body=body,
        grids=page_grids(page, page_number),
        words=page_words(page),
        height=page.rect.height,
        width=page.rect.width,
    )


def modal_size(lines: list[SourceLine]) -> float:
    counts: dict[float, int] = {}
    for line in lines:
        key = round(line.size_max, 1)
        counts[key] = counts.get(key, 0) + len(line.text.strip())
    if not counts:
        return 0.0
    # Most inked size wins; ties broken by the smaller size so a body of 12pt
    # never loses to a heading size that happens to tie.
    return sorted(counts, key=lambda s: (-counts[s], s))[0]


def build_blocks(pages: list[PageInput], rulebook: Rulebook) -> list[Block]:
    all_body = [l for p in pages for l in p.body]
    body_size = modal_size(all_body)
    min_left = min((l.bbox[0] for l in all_body), default=0.0)

    blocks: list[Block] = []
    open_block: Optional[Block] = None
    heading_styles: list[tuple[float, float]] = []   # (size, left) of depth-1 blocks
    last_heading_number: Optional[int] = None
    seen_numbered = False

    def close() -> None:
        nonlocal open_block
        if open_block is not None:
            _finalise(open_block)
            blocks.append(open_block)
            open_block = None

    for page in pages:
        in_grid: set[int] = set()
        grid_lines: dict[int, list[SourceLine]] = {}
        for gi, grid in enumerate(page.grids):
            for li, line in enumerate(page.body):
                if li in in_grid:
                    continue
                if grid.locate(line.bbox) is not None:
                    in_grid.add(li)
                    grid_lines.setdefault(gi, []).append(line)
        free = [l for li, l in enumerate(page.body) if li not in in_grid]
        visual = merge_visual_lines(free, page.page)

        elements: list[tuple[float, str, object]] = []
        for line in visual:
            elements.append((round(line.bbox[1], 2), "line", line))
        for gi, grid in enumerate(page.grids):
            elements.append((round(grid.rows[0], 2), "grid", (gi, grid)))
        elements.sort(key=lambda e: (e[0], 0 if e[1] == "grid" else 1))

        for _, kind, payload in elements:
            if kind == "grid":
                close()
                gi, grid = payload           # type: ignore[misc]
                cells, _outside = fill_cells(grid, grid_lines.get(gi, []), page.words)
                blocks.append(_table_block(len(blocks), grid, cells))
                continue

            line: VisualLine = payload       # type: ignore[assignment]
            text = line.text
            if not text.strip():
                continue

            match = rulebook.match(text)
            if match is None:
                expected = (last_heading_number + 1) if last_heading_number is not None else 1
                match = recover_heading(
                    text,
                    rulebook,
                    expected_number=expected,
                    style_matches=_heading_style_matches(line, heading_styles, body_size, min_left),
                )
            if match is not None:
                close()
                seen_numbered = True
                if match.depth == 1:
                    heading_styles.append((round(line.size_max, 1), round(line.left, 1)))
                    try:
                        last_heading_number = int(match.key.split(".")[0])
                    except ValueError:
                        pass
                open_block = _numbered_block(len(blocks), line, match, page, body_size)
                continue

            if not seen_numbered and line.size_max >= body_size * COVER_TITLE_RATIO:
                close()
                blocks.append(_simple_block(len(blocks), line, "part_title"))
                continue

            if open_block is not None and _continues(open_block, line):
                _append_line(open_block, line)
            else:
                close()
                open_block = _simple_block(len(blocks), line, "prose")
                open_block.heading_like = line.bold and line.size_max > body_size + 0.5
    close()
    return _stitch_tables(blocks)


def _continues(block: Block, line: VisualLine) -> bool:
    """Whether an unnumbered line is a wrapped continuation of the open block.

    Typography decides. A 12pt sentence is not the wrapped remainder of an 18pt
    heading, it is the heading's body: Core Terms sets "1. Definitions used in
    the contract" at 18pt and "Interpret this Contract using Joint Schedule 1
    (Definitions)." at 12pt beneath it, and gluing them would put a sentence in
    a title. Where a part sets its headings at body size and leans on weight
    instead, as Call-Off Schedule 9 does, weight carries the same decision.
    """
    if abs(line.size_max - block.size_max) >= 0.6:
        return False
    if block.heading_like and line.bold != block.bold:
        return False
    return True


def _heading_style_matches(
    line: VisualLine,
    heading_styles: list[tuple[float, float]],
    body_size: float,
    min_left: float,
) -> bool:
    if not line.bold:
        return False
    if heading_styles:
        size, left = heading_styles[-1]
        return abs(line.size_max - size) < 0.6 and abs(line.left - left) <= INDENT_TOLERANCE
    return line.size_max >= body_size - 0.1 and abs(line.left - min_left) <= INDENT_TOLERANCE


def _is_heading_like(rest: str, size: float, bold: bool, depth: int, body_size: float) -> bool:
    """Typography saying "this number carries a title, not a sentence".

    Three independent signals, any of which is enough, because the pack sets
    headings three different ways: Core Terms enlarges them (18pt tops, 14pt
    sub-headings, 12pt body), Call-Off Schedule 9 keeps body size and bolds
    them, and some schedules bold a short label with no terminal punctuation.
    Whether the node ends up a heading or a container with a lead-in is stage
    2's decision; this only reports what the ink looks like.
    """
    if size > body_size + 0.5:
        return True
    if not bold:
        return False
    if depth == 1:
        return True
    stripped = rest.rstrip()
    return len(stripped) <= 80 and not stripped.endswith((".", ";", ":", ","))


def _numbered_block(index: int, line: VisualLine, match, page: PageInput, body_size: float) -> Block:
    rest = line.text[match.rest_start:].strip()
    number_box = word_box_for_token(page.words, line, match.token)
    block = Block(
        index=index,
        block_kind="numbered",
        page_start=line.page,
        page_end=line.page,
        bboxes=[PageBox(page=line.page, bbox=line.bbox)],
        text=rest,
        lines=list(line.pieces),
        number=match.label,
        number_bbox=PageBox(page=line.page, bbox=number_box) if number_box else None,
        level=match.level,
        depth=match.depth,
        left=line.left,
        size_max=line.size_max,
        bold=line.bold,
        heading_like=_is_heading_like(rest, line.size_max, line.bold, match.depth, body_size),
    )
    if match.anomaly:
        block.anomalies.append(match.anomaly)
    if number_box is None:
        block.anomalies.append(
            f"numbering_box_not_located: token {match.token!r} not found as a word on page {line.page}"
        )
    return block


def _simple_block(index: int, line: VisualLine, kind: str) -> Block:
    return Block(
        index=index,
        block_kind=kind,
        page_start=line.page,
        page_end=line.page,
        bboxes=[PageBox(page=line.page, bbox=line.bbox)],
        text=line.text.strip(),
        lines=list(line.pieces),
        left=line.left,
        size_max=line.size_max,
        bold=line.bold,
    )


def _append_line(block: Block, line: VisualLine) -> None:
    previous = block.text
    addition = line.text.strip()
    if previous and addition:
        if _HYPHEN_BREAK.search(previous) and _LOWER_START.match(addition):
            note = (
                "line_break_after_hyphen: wrapped lines joined with a space, "
                "the hyphen is kept as printed"
            )
            if note not in block.anomalies:
                block.anomalies.append(note)
        block.text = previous + " " + addition
    else:
        block.text = previous or addition
    block.lines.extend(line.pieces)
    block.page_end = max(block.page_end, line.page)
    for existing in block.bboxes:
        if existing.page == line.page:
            block.bboxes[block.bboxes.index(existing)] = PageBox(
                page=line.page, bbox=union([existing.bbox, line.bbox])
            )
            break
    else:
        block.bboxes.append(PageBox(page=line.page, bbox=line.bbox))
    block.left = min(block.left, line.left) if block.left is not None else line.left


def _finalise(block: Block) -> None:
    block.text = block.text.strip()
    block.bboxes.sort(key=lambda b: b.page)
    block.lines.sort(key=lambda l: (l.page, round(l.bbox[1], 2), round(l.bbox[0], 2)))


def _table_block(index: int, grid: Grid, cells: list[Cell]) -> Block:
    form = is_form_grid(grid, cells)
    return Block(
        index=index,
        block_kind="table",
        page_start=grid.page,
        page_end=grid.page,
        bboxes=[PageBox(page=grid.page, bbox=grid.bbox)],
        text="",
        lines=[],
        left=grid.cols[0],
        table_rows=grid.n_rows,
        table_cols=grid.n_cols,
        grid_cols=list(grid.cols),
        cells=cells,
        heading_like=False,
        anomalies=["form_grid" ] if form else [],
    )


def _stitch_tables(blocks: list[Block]) -> list[Block]:
    """Join a table that continues onto the next page into one table.

    Consecutive table blocks on consecutive pages with the same column
    boundaries are one table interrupted by a page break: the definitions
    schedule runs that way for twenty-seven pages. The continuation's first row
    merges into the previous table's last row when it carries no label of its
    own, which is what reunites a definition whose value spills over the break.
    """
    out: list[Block] = []
    for block in blocks:
        if (
            block.block_kind == "table"
            and out
            and out[-1].block_kind == "table"
            and out[-1].page_end + 1 == block.page_start
            and _same_columns(out[-1].grid_cols, block.grid_cols)
        ):
            _merge_table(out[-1], block)
            continue
        out.append(block)
    for i, block in enumerate(out):
        block.index = i
    return out


def _same_columns(a: list[float], b: list[float]) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= INDENT_TOLERANCE for x, y in zip(a, b))


def _merge_table(target: Block, extra: Block) -> None:
    offset = target.table_rows
    continuation = _is_row_continuation(target, extra)
    if continuation:
        offset -= 1
    for cell in extra.cells:
        cell.row += offset
    if continuation:
        by_slot = {(c.row, c.col): c for c in target.cells}
        remaining: list[Cell] = []
        for cell in extra.cells:
            host = by_slot.get((cell.row, cell.col))
            if host is None:
                remaining.append(cell)
                continue
            if cell.text.strip():
                host.text = (host.text + " " + cell.text).strip() if host.text else cell.text
            host.lines.extend(cell.lines)
            for note in cell.anomalies:
                if note not in host.anomalies:
                    host.anomalies.append(note)
        extra.cells = remaining
        note = (
            f"row_continues_across_page_break: rows on pages "
            f"{target.page_end} and {extra.page_start} are one row"
        )
        if note not in target.anomalies:
            target.anomalies.append(note)
    target.cells.extend(extra.cells)
    target.table_rows = max((c.row for c in target.cells), default=-1) + 1
    target.page_end = extra.page_end
    target.bboxes.extend(extra.bboxes)
    note = f"table_stitched_across_pages: {target.page_start}-{target.page_end}"
    target.anomalies = [a for a in target.anomalies if not a.startswith("table_stitched_across_pages")]
    target.anomalies.append(note)


def _is_row_continuation(target: Block, extra: Block) -> bool:
    """The continuation's first row carries no label of its own."""
    first = [c for c in extra.cells if c.row == 0]
    if not first:
        return False
    labels = [c for c in first if c.col == 0]
    return all(not c.text.strip() for c in labels)
