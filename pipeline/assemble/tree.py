"""Stage 2: layout blocks to the provision tree.

Kind is decided by function, level by numbering. A number's dotted depth says
where it sits in the ladder; what it *is* depends on whether it holds text or
holds children, which is the branch-or-leaf rule from SPEC 2.1 and the reason
depth here is ragged. Three shapes come out of it.

- A bare grouping number, "3.1 All deliverables", holds a title and children
  and no sentence of its own. It becomes a heading: no text, no intro child.
- A container with a lead-in sentence and numbered children, "9.1 ... to enable
  it to both:" followed by (a) and (b), keeps its children and hands its lead-in
  to a first child of kind intro, citable false, path segment `intro`.
- Anything holding only words is a leaf and carries them as text.

Nothing is cleaned. Placeholders like "[Insert name...]" and the stray
character in "rFramework Contract" go in exactly as printed, with an anomaly
beside them where the parser can name what is odd.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from pipeline.parse.geometry import union
from pipeline.schemas import BBox, Node, content_hash, lineage_key, node_id

# The interpretation clause names Clause, Schedule, Part, Paragraph, Annex and
# Table and says nothing about lettered or roman items, so their label comes
# from the rulebook and the source of each label is recorded on the node.
ITEM_LABEL_SOURCE = "profile"
DOCUMENT_LABEL_SOURCE = "document"

_LEVEL_KINDS = {
    "heading": "heading",
    "clause": "clause",
    "subclause": "subclause",
    "item": "item",
}


@dataclass
class Context:
    document: str
    part_id: str
    version: str
    batch_id: Optional[str]
    unit_label: str
    printed_pages: dict[int, Optional[str]]
    anomalies: list[str] = field(default_factory=list)
    order: int = 0
    # path -> the box of the node's own printed number, kept so a container
    # that hands its words to an intro child still owns ink of its own.
    number_boxes: dict[str, BBox] = field(default_factory=dict)

    def next_order(self) -> int:
        n = self.order
        self.order += 1
        return n

    def printed(self, page: int) -> Optional[str]:
        return self.printed_pages.get(page)


def unit_label_for(part_id: str, family: Optional[str], profile: dict) -> tuple[str, str]:
    """Unit label for a part's own numbered provisions, and where it came from.

    Joint Schedule 1 paragraph 1.3.8 stipulates that Clauses and Schedules are
    the Core Terms' and that references inside a Schedule to parts, paragraphs,
    annexes and tables are that Schedule's, so a provision of the Core Terms is
    a Clause and the same shape inside a schedule is a Paragraph. Both labels
    come from the document; the rulebook only supplies the mapping's default for
    part families the interpretation clause does not name.
    """
    labels = profile.get("unit_labels", {})
    if part_id in labels:
        return labels[part_id], DOCUMENT_LABEL_SOURCE
    return labels.get("_schedule_default", "Paragraph"), DOCUMENT_LABEL_SOURCE


def build_part(layout: dict, profile: dict) -> tuple[Node, list[str]]:
    part = layout["part"]
    part_id = part["id"]
    unit_label, unit_source = unit_label_for(part_id, part.get("family"), profile)
    ctx = Context(
        document=layout["document"],
        part_id=part_id,
        version=part["template_version"],
        batch_id=part.get("batch_id"),
        unit_label=unit_label,
        printed_pages={p["page"]: p["printed_page"] for p in layout["pages"]},
    )

    blocks = list(layout["blocks"])
    # The part's cover-page title is the part's own ink, not a provision. Its
    # boxes go on the part node so page 1 is accounted for, and every line it
    # absorbed is named on the part so nothing is silently dropped.
    cover = [b for b in blocks if b["block_kind"] == "part_title"]
    cover_boxes: list[BBox] = []
    for block in cover:
        cover_boxes.extend(_boxes_from(block))
    root = _make_node(
        ctx,
        path=part_id,
        kind="part",
        page_start=part["page_start"],
        page_end=part["page_end"],
        own_boxes=_merge_boxes(cover_boxes),
        title=part["title"],
        unit_label=unit_label,
        unit_label_source=unit_source,
        anomalies=list(part.get("anomalies", [])),
        part_family=part.get("family"),
        template_version=part["template_version"],
    )
    if cover:
        root.anomalies.append(
            "cover_title_absorbed: the part's cover-page title lines are held as "
            "the part's own boxes, printed as "
            + " | ".join(repr(b["text"]) for b in cover)
        )

    stack: list[tuple[int, Node]] = [(0, root)]
    pending_prose: list[dict] = []
    last_top: Optional[int] = None
    scope_count = 0

    for block in blocks:
        kind = block["block_kind"]
        if kind == "part_title":
            continue
        if kind == "prose":
            pending_prose.append(block)
            continue

        if block["block_kind"] == "numbered" and (block["depth"] or 1) == 1:
            top = _top_number(block)
            if top is not None and last_top is not None and top <= last_top:
                # The part's numbering has restarted. Call-Off Schedule 9 runs
                # paragraphs 1 to 9 and then opens "Part B - Annex 1: Baseline
                # security requirements" and starts again at 1. Without a scope
                # of its own the annex's paragraph 3.2 would take the same path
                # as the body's, and one path would name two provisions.
                scope_count += 1
                title_block = _last_heading_like(pending_prose)
                if title_block is not None:
                    pending_prose = [p for p in pending_prose if p is not title_block]
                scope = _scope_node(ctx, root, title_block, scope_count)
                _flush_prose(ctx, root, pending_prose, has_more_children=True)
                pending_prose = []
                root.children.append(scope)
                stack = [(0, root), (0, scope)]
                last_top = None
            if top is not None:
                last_top = top
        if kind == "table":
            parent = stack[-1][1]
            _flush_prose(ctx, parent, pending_prose, has_more_children=True)
            pending_prose = []
            _attach_table(ctx, parent, block, profile)
            continue

        depth = block["depth"] or 1
        while len(stack) > 1 and stack[-1][0] >= depth:
            node = stack.pop()[1]
            _flush_prose(ctx, node, pending_prose, has_more_children=False)
            pending_prose = []
        parent = stack[-1][1]
        _flush_prose(ctx, parent, pending_prose, has_more_children=True)
        pending_prose = []
        node = _node_for_block(ctx, parent, block)
        parent.children.append(node)
        stack.append((depth, node))

    while stack:
        node = stack.pop()[1]
        _flush_prose(ctx, node, pending_prose, has_more_children=False)
        pending_prose = []

    _finalise(root, ctx)
    return root, ctx.anomalies


def _make_node(
    ctx: Context,
    path: str,
    kind: str,
    page_start: int,
    page_end: int,
    own_boxes: list[BBox],
    text: Optional[str] = None,
    title: Optional[str] = None,
    label: Optional[str] = None,
    unit_label: Optional[str] = None,
    unit_label_source: Optional[str] = None,
    citable: bool = True,
    anomalies: Optional[list[str]] = None,
    **extra,
) -> Node:
    return Node(
        id=node_id(ctx.document, ctx.version, path),
        lineage_key=lineage_key(ctx.document, path),
        content_hash=content_hash(text) if text is not None else None,
        path=path,
        kind=kind,
        unit_label=unit_label,
        unit_label_source=unit_label_source,
        citable=citable,
        label=label,
        title=title,
        text=text,
        page_start=page_start,
        page_end=page_end,
        printed_page=ctx.printed(page_start),
        bboxes_own=own_boxes,
        bboxes_extent=list(own_boxes),
        order=ctx.next_order(),
        anomalies=list(anomalies or []),
        batch_id=ctx.batch_id,
        **extra,
    )


def _boxes_from(block: dict) -> list[BBox]:
    return [BBox(page=b["page"], bbox=tuple(b["bbox"])) for b in block["bboxes"]]


def _node_for_block(ctx: Context, parent: Node, block: dict) -> Node:
    level = block["level"] or "clause"
    kind = _LEVEL_KINDS.get(level, "clause")
    key = _path_key(block)
    path = f"{parent.path}/{key}"
    heading_like = bool(block["heading_like"])
    text = block["text"].strip()

    if block["depth"] == 4:
        unit_label, source = "Paragraph", ITEM_LABEL_SOURCE
    else:
        unit_label, source = ctx.unit_label, DOCUMENT_LABEL_SOURCE

    node = _make_node(
        ctx,
        path=path,
        kind="heading" if heading_like else kind,
        page_start=block["page_start"],
        page_end=block["page_end"],
        own_boxes=_boxes_from(block),
        text=None if heading_like else (text or None),
        title=text or None if heading_like else None,
        label=block["number"],
        unit_label=unit_label,
        unit_label_source=source,
        anomalies=list(block["anomalies"]),
    )
    if not heading_like and not text:
        node.anomalies.append(
            f"empty_provision_text: {block['number']} carries a number but no words"
        )
    if block.get("number_bbox"):
        ctx.number_boxes[path] = BBox(
            page=block["number_bbox"]["page"], bbox=tuple(block["number_bbox"]["bbox"])
        )
    return node


def _number_boxes(node: Node, ctx: Context) -> list[BBox]:
    box = ctx.number_boxes.get(node.path)
    return [box] if box else []


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
# Units the interpretation clause names, so a scope titled "Annex 1" or
# "Part B" takes its label from the document rather than from the rulebook.
_DOCUMENT_UNITS = ("Part", "Annex", "Schedule", "Table", "Paragraph", "Clause")


def _top_number(block: dict) -> Optional[int]:
    label = (block.get("number") or "").strip("()")
    head = label.split(".")[0]
    return int(head) if head.isdigit() else None


def _last_heading_like(prose: list[dict]) -> Optional[dict]:
    for block in reversed(prose):
        if block.get("heading_like"):
            return block
    return None


def _scope_node(ctx: Context, root: Node, title_block: Optional[dict], index: int) -> Node:
    title = (title_block or {}).get("text", "").strip() or None
    slug = _SLUG_STRIP.sub("-", title.lower()).strip("-") if title else ""
    key = slug or f"section-{index + 1}"
    unit_label, source = ctx.unit_label, DOCUMENT_LABEL_SOURCE
    if title:
        first = title.split()[0].rstrip(":,")
        for unit in _DOCUMENT_UNITS:
            if first.lower() == unit.lower():
                unit_label = unit
                break
    boxes = _boxes_from(title_block) if title_block else []
    node = _make_node(
        ctx,
        path=f"{root.path}/{key}",
        kind="heading",
        page_start=title_block["page_start"] if title_block else root.page_start,
        page_end=title_block["page_end"] if title_block else root.page_end,
        own_boxes=boxes,
        title=title,
        unit_label=unit_label,
        unit_label_source=source,
    )
    node.anomalies.append(
        "numbering_restarts_in_part: top-level numbering starts again here, so this "
        "section is a scope of its own and its provisions do not share paths with "
        "the ones before it"
        + (f"; the section is titled {title!r}" if title else "; the section prints no title")
    )
    return node


def _path_key(block: dict) -> str:
    number = block["number"] or ""
    return number.strip("()") if number.startswith("(") else number


def _flush_prose(ctx: Context, parent: Node, prose: list[dict], has_more_children: bool) -> None:
    """Attach unnumbered prose sitting under `parent`.

    With numbered children following it, it is a lead-in and becomes an intro
    child: citable false, path segment `intro`, so the container's own text
    stays null and every character on the page still has exactly one owner.
    With nothing following, the container is a leaf and simply carries the
    words.
    """
    if not prose:
        return
    text = " ".join(p["text"].strip() for p in prose if p["text"].strip())
    if not text:
        return
    boxes: list[BBox] = []
    for block in prose:
        boxes.extend(_boxes_from(block))
    anomalies: list[str] = []
    for block in prose:
        anomalies.extend(block["anomalies"])
    page_start = min(b.page for b in boxes)
    page_end = max(b.page for b in boxes)

    existing = [c for c in parent.children if c.kind != "ref"]
    if not existing and not has_more_children and parent.kind != "part":
        if parent.text is None:
            parent.text = text
            parent.content_hash = content_hash(text)
            parent.bboxes_own = _merge_boxes(parent.bboxes_own + boxes)
            parent.page_end = max(parent.page_end, page_end)
            parent.anomalies.extend(a for a in anomalies if a not in parent.anomalies)
            return

    if parent.kind == "part":
        # Prose before the part's first numbered provision is the part's
        # opening words: a preamble, citable in its own right.
        node = _make_node(
            ctx,
            path=f"{parent.path}/preamble" if not existing else f"{parent.path}/preamble-{len(existing)}",
            kind="preamble",
            page_start=page_start,
            page_end=page_end,
            own_boxes=_merge_boxes(boxes),
            text=text,
            unit_label=ctx.unit_label,
            unit_label_source=DOCUMENT_LABEL_SOURCE,
            anomalies=anomalies,
        )
        parent.children.append(node)
        return

    if parent.text is not None:
        # The container already holds words and is about to hold children too:
        # move its words into the intro so the branch-or-leaf rule holds.
        text = (parent.text + " " + text).strip()
        boxes = _merge_boxes(parent.bboxes_own + boxes)
        parent.text = None
        parent.content_hash = None
        page_start = min(b.page for b in boxes)
        page_end = max(b.page for b in boxes)

    suffix = "intro" if not any(c.path.endswith("/intro") for c in existing) else f"intro-{len(existing)}"
    node = _make_node(
        ctx,
        path=f"{parent.path}/{suffix}",
        kind="intro",
        page_start=page_start,
        page_end=page_end,
        own_boxes=_merge_boxes(boxes),
        text=text,
        citable=False,
        anomalies=anomalies,
    )
    parent.children.insert(0, node)


def _merge_boxes(boxes: list[BBox]) -> list[BBox]:
    by_page: dict[int, list] = {}
    for box in boxes:
        by_page.setdefault(box.page, []).append(box.bbox)
    return [
        BBox(page=page, bbox=union(by_page[page]))
        for page in sorted(by_page)
    ]


def _attach_table(ctx: Context, parent: Node, block: dict, profile: dict) -> None:
    is_form = any(a.startswith("form_grid") for a in block["anomalies"])
    notes = [a for a in block["anomalies"] if not a.startswith("form_grid")]
    if is_form:
        _attach_form_rows(ctx, parent, block, notes)
        return

    path = f"{parent.path}/table" if not any(
        c.path.endswith("/table") for c in parent.children
    ) else f"{parent.path}/table-{len(parent.children)}"
    table = _make_node(
        ctx,
        path=path,
        kind="table",
        page_start=block["page_start"],
        page_end=block["page_end"],
        own_boxes=[],
        unit_label="Table",
        unit_label_source=DOCUMENT_LABEL_SOURCE,
        anomalies=notes,
        n_rows=block["table_rows"],
        n_cols=block["table_cols"],
    )
    for cell in block["cells"]:
        table.children.append(_cell_node(ctx, table.path, cell))
    parent.children.append(table)


def _attach_form_rows(ctx: Context, parent: Node, block: dict, notes: list[str]) -> None:
    """A form is numbered rows of label and value cells, not a clause tree."""
    by_row: dict[int, list[dict]] = {}
    for cell in block["cells"]:
        by_row.setdefault(cell["row"], []).append(cell)
    number_col = 0
    for row in sorted(by_row):
        cells = sorted(by_row[row], key=lambda c: c["col"])
        inked = [c for c in cells if c["text"].strip()]
        if not inked:
            continue
        number = next(
            (c["text"].strip().rstrip(".") for c in cells if c["col"] == number_col and c["text"].strip()),
            None,
        )
        key = number if number else f"row-{row}"
        path = f"{parent.path}/{key}"
        if any(c.path == path for c in parent.children):
            path = f"{path}-{row}"
        boxes = _merge_boxes(
            [BBox(page=c["page"], bbox=tuple(c["bbox"])) for c in inked]
        )
        row_node = _make_node(
            ctx,
            path=path,
            kind="form_row",
            page_start=min(b.page for b in boxes),
            page_end=max(b.page for b in boxes),
            own_boxes=[],
            label=number,
            anomalies=list(notes) if row == min(by_row) else [],
        )
        if number is None:
            row_node.anomalies.append(
                f"form_row_without_number: row {row} of the form prints no row number"
            )
        for cell in cells:
            if cell["col"] == number_col:
                continue
            if not cell["text"].strip() and len(inked) > 1:
                continue
            role = cell["role"]
            suffix = role if not any(
                c.path.endswith(f"/{role}") for c in row_node.children
            ) else f"{role}-{cell['col']}"
            row_node.children.append(_cell_node(ctx, row_node.path, cell, suffix=suffix))
        if not row_node.children:
            continue
        parent.children.append(row_node)


def _cell_node(ctx: Context, parent_path: str, cell: dict, suffix: Optional[str] = None) -> Node:
    key = suffix if suffix else f"{cell['row']}/{cell['col']}"
    node = _make_node(
        ctx,
        path=f"{parent_path}/{key}",
        kind="cell",
        page_start=cell["page"],
        page_end=cell["page"],
        own_boxes=[BBox(page=cell["page"], bbox=tuple(cell["bbox"]))],
        text=cell["text"],
        anomalies=list(cell["anomalies"]),
        row=cell["row"],
        col=cell["col"],
        cell_role=cell["role"],
        role_confidence=cell["role_confidence"],
    )
    return node


def _finalise(node: Node, ctx: Context) -> None:
    """Bottom-up: extents, page ranges, and the branch-or-leaf guarantee."""
    children = [c for c in node.children if c.kind != "ref"]
    for child in children:
        _finalise(child, ctx)

    if children and node.text is not None:
        # The branch-or-leaf rule: a container that holds a lead-in sentence
        # and numbered children hands the lead-in to an intro child, so the
        # container's own text stays null and every character on the page has
        # exactly one owner. This is the specified shape, not an anomaly.
        intro = _make_node(
            ctx,
            path=f"{node.path}/intro",
            kind="intro",
            page_start=node.page_start,
            page_end=node.page_end,
            own_boxes=list(node.bboxes_own),
            text=node.text,
            citable=False,
        )
        node.children.insert(0, intro)
        node.text = None
        node.content_hash = None
        # What stays the container's own ink is its printed number. That is the
        # only mark on the page that belongs to the container rather than to
        # one of its children, and keeping it is what lets the left-edge
        # invariant compare a child's indent against its parent's rather than
        # against a box that already contains the child.
        node.bboxes_own = _number_boxes(node, ctx)
        children = [c for c in node.children if c.kind != "ref"]

    boxes = list(node.bboxes_own)
    for child in children:
        boxes.extend(child.bboxes_extent)
    node.bboxes_extent = _merge_boxes(boxes)
    if node.bboxes_extent and node.kind != "part":
        node.page_start = min(b.page for b in node.bboxes_extent)
        node.page_end = max(b.page for b in node.bboxes_extent)
        node.printed_page = ctx.printed(node.page_start)
    # A part covers its whole derived page range, including any page whose ink
    # produced no node. Shrinking it to the pages that happened to parse would
    # quietly lose the fact that the part runs to page 60.
    if node.kind in ("document", "part", "form_row", "table"):
        node.text = None
        node.content_hash = None


def renumber(root: Node) -> None:
    """`order` is the node's preorder position within its part."""
    counter = [0]

    def walk(node: Node) -> None:
        node.order = counter[0]
        counter[0] += 1
        for child in node.children:
            if child.kind != "ref":
                walk(child)

    walk(root)
