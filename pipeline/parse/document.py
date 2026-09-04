"""One scan of the PDF, shared by stage 0 and stage 1.

Furniture repetition is a document-level fact, and part boundaries are a
document-level fact derived from it, so both stages open the file once and read
the same scan. Nothing here consults the embedded outline or the notes' page
map: the derived page map is derived, and cross-checking it against those two
artifacts is stage 8's job.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pymupdf

from .furniture import PageFurniture, count_repetitions, split_page
from .model import SourceLine
from .parts import PartRun, canonicalise_ids, detect_parts
from .words import font_size_histogram, page_source_lines


@dataclass
class PageScan:
    page: int
    width: float
    height: float
    lines: list[SourceLine]
    furniture: PageFurniture
    n_images: int
    image_area: float
    n_drawings: int

    @property
    def body_chars(self) -> int:
        return sum(len(l.text.strip()) for l in self.furniture.body)

    @property
    def has_text_layer(self) -> bool:
        return any(l.text.strip() for l in self.lines)


@dataclass
class DocumentScan:
    path: Path
    page_count: int
    sha256: str
    metadata: dict
    tagged: bool
    has_outline: bool
    pages: dict[int, PageScan]
    parts: list[PartRun] = field(default_factory=list)
    part_id_renames: dict[str, str] = field(default_factory=dict)

    def part_by_id(self, part_id: str) -> Optional[PartRun]:
        for part in self.parts:
            if part.slug == part_id:
                return part
        return None

    def font_histogram(self) -> dict[str, int]:
        every: list[SourceLine] = []
        for page in sorted(self.pages):
            every.extend(self.pages[page].lines)
        return font_size_histogram(every)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan(pdf_path: Path, batches: dict, page_range: Optional[tuple[int, int]] = None) -> DocumentScan:
    doc = pymupdf.open(pdf_path)
    lo, hi = page_range or (1, doc.page_count)
    lo = max(1, lo)
    hi = min(doc.page_count, hi)

    raw: dict[int, list[SourceLine]] = {}
    heights: dict[int, float] = {}
    meta: dict[int, tuple[float, float, int, float, int]] = {}
    for page_no in range(lo, hi + 1):
        page = doc[page_no - 1]
        raw[page_no] = page_source_lines(page, page_no)
        heights[page_no] = page.rect.height
        images = page.get_images(full=True)
        image_area = 0.0
        for info in page.get_image_info():
            box = info.get("bbox")
            if box:
                image_area += max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
        meta[page_no] = (
            page.rect.width,
            page.rect.height,
            len(images),
            image_area / max(1.0, page.rect.width * page.rect.height),
            len(page.get_drawings()),
        )

    repetitions = count_repetitions(raw, heights)
    pages: dict[int, PageScan] = {}
    for page_no in range(lo, hi + 1):
        width, height, n_images, image_area, n_drawings = meta[page_no]
        pages[page_no] = PageScan(
            page=page_no,
            width=width,
            height=height,
            lines=raw[page_no],
            furniture=split_page(page_no, raw[page_no], height, repetitions),
            n_images=n_images,
            image_area=image_area,
            n_drawings=n_drawings,
        )

    signatures = [
        (
            page_no,
            pages[page_no].furniture.header_title,
            pages[page_no].furniture.model_version_raw,
            pages[page_no].furniture.header_version_raw,
            pages[page_no].furniture.project_version_raw,
        )
        for page_no in range(lo, hi + 1)
    ]
    parts = detect_parts(signatures)
    renames = canonicalise_ids(parts, batches)

    catalog = doc.pdf_catalog()
    mark_info = doc.xref_get_key(catalog, "MarkInfo") if catalog else (None, None)
    tagged = bool(mark_info and mark_info[1] and "Marked" in str(mark_info[1]) and "true" in str(mark_info[1]).lower())

    result = DocumentScan(
        path=pdf_path,
        page_count=doc.page_count,
        sha256=sha256_of(pdf_path),
        metadata=dict(doc.metadata or {}),
        tagged=tagged,
        # Existence only. The outline's contents are a stage 8 cross-check input
        # and reading them here would be a spec violation.
        has_outline=bool(doc.outline),
        pages=pages,
        parts=parts,
        part_id_renames=renames,
    )
    doc.close()
    return result
