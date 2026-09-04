"""Stage 1: build one layout file per part."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pymupdf

from .blocks import PageInput, build_blocks, collect_page
from .document import DocumentScan
from .furniture import normalise_version
from .model import LayoutFile, PageInfo, PartInfo
from .numbering import Rulebook
from .parts import PartRun


def batch_for_part(part_id: str, batches: dict) -> Optional[str]:
    for batch in sorted(batches):
        if batches[batch]["part"] == part_id:
            return batch
    return None


def build_layout(
    pdf_path: Path,
    scan: DocumentScan,
    part: PartRun,
    rulebook: Rulebook,
    document_id: str,
    batches: dict,
) -> LayoutFile:
    doc = pymupdf.open(pdf_path)
    inputs: list[PageInput] = []
    page_infos: list[PageInfo] = []
    for page_no in range(part.page_start, part.page_end + 1):
        page_scan = scan.pages[page_no]
        inputs.append(collect_page(doc[page_no - 1], page_no, page_scan.furniture.body))
        page_infos.append(
            PageInfo(
                page=page_no,
                width=page_scan.width,
                height=page_scan.height,
                printed_page=page_scan.furniture.printed_page,
                furniture=list(page_scan.furniture.stripped),
                body_chars=page_scan.body_chars,
                route="text_layer" if page_scan.has_text_layer else "no_text_layer",
            )
        )
    doc.close()

    blocks = build_blocks(inputs, rulebook)

    raw_version = part.model_version_raw or part.header_version_raw
    version_key, changed = normalise_version(raw_version)
    anomalies = list(part.anomalies)
    if raw_version is None:
        anomalies.append(
            "template_version_absent: the part's furniture names no Model Version "
            "or Version, node ids use version 'v0'"
        )
    elif changed:
        anomalies.append(
            f"template_version_normalised: printed {raw_version!r}, key {version_key!r}, "
            "the printed form is kept in template_version_raw"
        )
    if not any(p.printed_page for p in page_infos):
        anomalies.append("printed_page_absent: no page in this part prints a page number")

    part_info = PartInfo(
        id=part.slug,
        title=part.title,
        family=part.family,
        page_start=part.page_start,
        page_end=part.page_end,
        template_version=version_key,
        template_version_raw=raw_version,
        template_version_source=part.version_source,
        project_version_raw=part.project_version_raw,
        slug_source="config_batch" if part.slug in scan.part_id_renames.values() else "derived",
        batch_id=batch_for_part(part.slug, batches),
        anomalies=anomalies,
    )
    return LayoutFile(
        document=document_id,
        profile=rulebook.name,
        part=part_info,
        pages=page_infos,
        blocks=blocks,
    )
