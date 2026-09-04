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

import math
import re
from dataclasses import dataclass, field
from typing import Optional

import pymupdf

from .geometry import INDENT_TOLERANCE, union
from .model import Block, Cell, PageBox, SourceLine
from .numbering import Rulebook, recover_heading
from .tables import Grid, fill_cells, is_form_grid, page_grids
from .words import VisualLine, merge_visual_lines, page_words, word_box_for_token

# A cover title is set far larger than the body it introduces.
COVER_TITLE_RATIO = 1.4

# Left edges within this much of each other are the same indent.
INDENT_CLUSTER = 3.0
# An indent carries a level only if this share of that level's candidates use
# it. Measured across the four batch parts: Core Terms sets 144 of 146 clauses
# at x=27 and 2 elsewhere, Call-Off Schedule 9 sets its subclauses at three
# genuine indents (58, 117, 119), and Core Terms sets headings 1 and 2 at x=27
# and the other 33 at x=30. So a real second indent is not rare, and only the
# true singletons need to fall out.
INDENT_SUPPORT_SHARE = 0.05

_HYPHEN_BREAK = re.compile(r"-$")
_LOWER_START = re.compile(r"^[a-z]")


@dataclass
class IndentSupport:
    """Which left edges each numbering level actually uses in this part."""
    clusters: dict[str, list[tuple[float, int]]] = field(default_factory=dict)

    def supported(self, level: str, left: float) -> bool:
        clusters = self.clusters.get(level)
        if not clusters:
            return True
        total = sum(count for _, count in clusters)
        floor = max(1, math.ceil(INDENT_SUPPORT_SHARE * total))
        return any(
            abs(left - centre) <= INDENT_CLUSTER and count >= floor
            for centre, count in clusters
        )


def measure_indents(candidates: list[tuple[str, float]]) -> IndentSupport:
    grouped: dict[str, dict[float, int]] = {}
    for level, left in candidates:
        buckets = grouped.setdefault(level, {})
        key = min(
            (k for k in buckets if abs(k - left) <= INDENT_CLUSTER),
            key=lambda k: (abs(k - left), k),
            default=round(left, 1),
        )
        buckets[key] = buckets.get(key, 0) + 1
    return IndentSupport(
        clusters={
            level: sorted(buckets.items())
            for level, buckets in sorted(grouped.items())
        }
    )


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


def _candidate_indents(pages: list[PageInput], rulebook: Rulebook) -> IndentSupport:
    """First pass: which indents each level actually uses in this part.

    Lines inside a ruled grid are skipped, exactly as the second pass skips
    them. A table cell is full of its own lettered lists — Joint Schedule 1's
    definition of "Auditor" runs a) to j) inside one cell — and letting those
    into the indent clusters would make the two real items in the schedule's
    own prose look like an indent no item uses, and lose them.
    """
    found: list[tuple[str, float]] = []
    for page in pages:
        in_grid = {
            id(line)
            for grid in page.grids
            for line in page.body
            if grid.locate(line.bbox) is not None
        }
        free = [l for l in page.body if id(l) not in in_grid]
        for line in merge_visual_lines(free, page.page):
            text = line.text
            if not text.strip():
                continue
            match = rulebook.match(text)
            if match is not None:
                found.append((match.level, line.left))
    return measure_indents(found)


def _duplicates_a_sibling(match, stack: list[tuple[int, Block, set]]) -> bool:
    """Whether this number already names a provision under the same parent.

    A provision number does not repeat within its parent, so a second "27.1"
    under heading 27 is a citation to the first, not a new clause. Core Terms
    27.3 wraps onto a line opening "27.1 or 27.2 or has any reason to think",
    and 27.1 is already a sibling by then. Exact rather than heuristic, which
    is why it replaced an earlier guess about unfinished sentences that also
    swallowed genuine items whose clause happened to end a line on a URL.
    """
    depth = match.depth
    for entry_depth, _block, labels in reversed(stack):
        if entry_depth == depth - 1:
            return match.label in labels
    return False


def _numeric_parent_ok(match, stack: list[tuple[int, Block, set]]) -> Optional[str]:
    """Whether a dotted number nests under the number currently above it.

    Returns None when it does, otherwise the enclosing number it disagrees
    with. This is the "children nest under their numeric parents" invariant
    applied at parse time, and it is what catches a wrapped line that happens
    to begin with a cross-reference: "... 10.4.4, 10.4.5, 20.2 or a Contract
    expires all of the following apply:" wraps onto a line starting "20.2"
    while clause 10.6 is open, and 20.2 does not belong under heading 10.
    """
    if match.dotted_depth <= 1:
        return None
    prefix = match.key.rsplit(".", 1)[0]
    for depth, block, _labels in reversed(stack):
        if depth == match.depth - 1:
            if block.number and block.number.strip("()") == prefix:
                return None
            return block.number
    return None


def _at_wrap_indent(block: Block, line: VisualLine) -> bool:
    """Whether the line sits exactly where the open block's wrapped lines sit.

    A new provision does not begin at the indent its predecessor wraps to. Core
    Terms clause 27.3 wraps to x=55.4 and one of its wrapped lines opens with
    "27.1 or 27.2 or has any reason to think ..."; the number is a citation, and
    the line is at 55.4 where every other wrapped line of that clause sits.
    On its own this proves nothing, because Core Terms also sets its lettered
    items at 55.4, so it only counts alongside an indent no provision of that
    level uses.
    """
    tops = sorted({round(l.bbox[1], 2) for l in block.lines})
    if len(tops) < 2:
        return False
    first_top = tops[0]
    wrapped = [l.bbox[0] for l in block.lines if round(l.bbox[1], 2) > first_top]
    return bool(wrapped) and abs(line.left - min(wrapped)) <= INDENT_TOLERANCE


def build_blocks(pages: list[PageInput], rulebook: Rulebook) -> list[Block]:
    all_body = [l for p in pages for l in p.body]
    body_size = modal_size(all_body)
    min_left = min((l.bbox[0] for l in all_body), default=0.0)
    indents = _candidate_indents(pages, rulebook)

    blocks: list[Block] = []
    open_block: Optional[Block] = None
    # (depth, block, labels of its children so far)
    stack: list[tuple[int, Block, set[str]]] = []
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
                disagrees_with = _numeric_parent_ok(match, stack)
                unsupported = not indents.supported(match.level, line.left)
                at_wrap = open_block is not None and _at_wrap_indent(open_block, line)
                duplicate = _duplicates_a_sibling(match, stack)
                if unsupported and open_block is not None and (
                    match.depth == 1 or disagrees_with is not None or at_wrap or duplicate
                ):
                    # Not a provision: a wrapped line that opens with a number.
                    # It rejoins the block it was wrapping from, with its own
                    # leading number intact, and the decision is recorded.
                    _append_line(open_block, line)
                    note = (
                        f"numbering_read_as_wrapped_text: a line starting {match.label!r} "
                        f"sits at x={line.left:.1f}, an indent no {match.level} in this part "
                        f"uses, and does not nest under {disagrees_with or 'the open provision'}"
                    )
                    if note not in open_block.anomalies:
                        open_block.anomalies.append(note)
                    continue
                close()
                seen_numbered = True
                if match.depth == 1:
                    heading_styles.append((round(line.size_max, 1), round(line.left, 1)))
                    try:
                        last_heading_number = int(match.key.split(".")[0])
                    except ValueError:
                        pass
                open_block = _numbered_block(len(blocks), line, match, page, body_size)
                if disagrees_with is not None:
                    open_block.anomalies.append(
                        f"numbering_sequence_break: {match.label} follows {disagrees_with}, "
                        f"which is not its numeric parent"
                    )
                while stack and stack[-1][0] >= match.depth:
                    stack.pop()
                if stack:
                    stack[-1][2].add(match.label)
                stack.append((match.depth, open_block, set()))
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
        number_printed=match.token,
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
