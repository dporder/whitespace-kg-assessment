"""Stage 1 layout contract, `output/<run>/layout/<part>.json`.

`pipeline/schemas.py` is frozen and models stages 2 onward, so the layout file
between stage 1 and stage 2 needs its own declared shape. It is deliberately
lossless with respect to the text layer: every block keeps the raw source lines
it was reflowed from, every furniture line that was stripped is listed with its
box, and the raw printed forms of the versions are kept beside their normalised
keys. Nothing downstream needs to reopen the PDF to see what the ink said.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .geometry import Box, round_box

LAYOUT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class PageBox:
    page: int
    bbox: Box

    def as_json(self) -> dict:
        return {"page": self.page, "bbox": list(round_box(self.bbox))}


@dataclass
class SourceLine:
    """One line as the PDF text layer emitted it, before any reflow."""
    page: int
    bbox: Box
    text: str
    size_max: float
    bold: bool

    def as_json(self) -> dict:
        return {
            "page": self.page,
            "bbox": list(round_box(self.bbox)),
            "text": self.text,
            "size_max": round(self.size_max, 2),
            "bold": self.bold,
        }


@dataclass
class Cell:
    row: int
    col: int
    text: str
    page: int
    bbox: Box
    role: str                       # label | value | header
    role_confidence: float
    lines: list[SourceLine] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "row": self.row,
            "col": self.col,
            "text": self.text,
            "page": self.page,
            "bbox": list(round_box(self.bbox)),
            "role": self.role,
            "role_confidence": self.role_confidence,
            "lines": [l.as_json() for l in self.lines],
            "anomalies": list(self.anomalies),
        }


@dataclass
class Block:
    """One unit of ink in reading order: a numbered provision, a run of
    unnumbered prose, a part title, or a table with its cells."""
    index: int
    block_kind: str                 # numbered | prose | part_title | table
    page_start: int
    page_end: int
    bboxes: list[PageBox]
    text: str                       # furniture stripped, source lines reflowed
    lines: list[SourceLine] = field(default_factory=list)
    number: Optional[str] = None            # "3.1.2", "(a)", "2"
    number_bbox: Optional[PageBox] = None
    level: Optional[str] = None             # heading | clause | subclause | item
    depth: Optional[int] = None             # 1..4, index into the rulebook levels
    left: Optional[float] = None            # left edge of the block's own ink
    size_max: float = 0.0
    bold: bool = False
    heading_like: bool = False
    table_rows: int = 0
    table_cols: int = 0
    grid_cols: list[float] = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "index": self.index,
            "block_kind": self.block_kind,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "bboxes": [b.as_json() for b in self.bboxes],
            "text": self.text,
            "lines": [l.as_json() for l in self.lines],
            "number": self.number,
            "number_bbox": self.number_bbox.as_json() if self.number_bbox else None,
            "level": self.level,
            "depth": self.depth,
            "left": round(self.left, 2) if self.left is not None else None,
            "size_max": round(self.size_max, 2),
            "bold": self.bold,
            "heading_like": self.heading_like,
            "table_rows": self.table_rows,
            "table_cols": self.table_cols,
            "grid_cols": [round(c, 2) for c in self.grid_cols],
            "cells": [c.as_json() for c in self.cells],
            "anomalies": list(self.anomalies),
        }


@dataclass
class PageInfo:
    page: int                       # absolute, 1-based
    width: float
    height: float
    printed_page: Optional[str]
    furniture: list[SourceLine] = field(default_factory=list)
    body_chars: int = 0
    route: str = "text_layer"

    def as_json(self) -> dict:
        return {
            "page": self.page,
            "width": round(self.width, 2),
            "height": round(self.height, 2),
            "printed_page": self.printed_page,
            "furniture": [l.as_json() for l in self.furniture],
            "body_chars": self.body_chars,
            "route": self.route,
        }


@dataclass
class PartInfo:
    id: str
    title: str                      # verbatim header title, whitespace-trimmed only
    family: Optional[str]
    page_start: int
    page_end: int
    template_version: str           # normalised key, "v3.0.11"
    template_version_raw: Optional[str]      # exactly as printed, "3.0.11"
    template_version_source: Optional[str]   # "footer" | "header" | None
    project_version_raw: Optional[str]
    slug_source: str                # "derived" | "config_batch"
    batch_id: Optional[str]
    anomalies: list[str] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "family": self.family,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "template_version": self.template_version,
            "template_version_raw": self.template_version_raw,
            "template_version_source": self.template_version_source,
            "project_version_raw": self.project_version_raw,
            "slug_source": self.slug_source,
            "batch_id": self.batch_id,
            "anomalies": list(self.anomalies),
        }


@dataclass
class LayoutFile:
    document: str
    profile: str
    part: PartInfo
    pages: list[PageInfo]
    blocks: list[Block]
    anomalies: list[str] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "layout_schema_version": LAYOUT_SCHEMA_VERSION,
            "document": self.document,
            "profile": self.profile,
            "part": self.part.as_json(),
            "pages": [p.as_json() for p in self.pages],
            "blocks": [b.as_json() for b in self.blocks],
            "anomalies": list(self.anomalies),
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_json(self.as_json()), encoding="utf-8")


def dump_json(obj: Any) -> str:
    """One JSON writer for every stage output: no timestamps, stable key order
    as inserted, unicode preserved, trailing newline."""
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def load_layout(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def asdict_safe(obj: Any) -> dict:
    return asdict(obj)
