"""Page-image crops, rendered from a stored bbox at request time.

Nothing is pre-baked and nothing is written to the repo: the PDF is read-only
(SPEC ground rule 0 forbids copying document content out of it except into
output/), so a crop exists only as the bytes of one HTTP response.

The box is drawn on a one-page copy of the source page, never on the cached
document, so repeated requests cannot accumulate ink.
"""
from __future__ import annotations

import threading

import pymupdf

from . import config as ui_config

PDF = ui_config.pipeline_config.PDF

# Trust-gradient colours from diagram/Main.dc.html, as PDF 0..1 triples.
BOX_COLOURS = {
    "deterministic": (0.247, 0.388, 0.588),   # #3f6396 refs, structure
    "rule": (0.180, 0.451, 0.451),            # #2e7373 term uses
    "model": (0.631, 0.420, 0.086),           # #a16b16 model-derived readings
    "human": (0.459, 0.345, 0.608),           # #75589b a human decision
    "ink": (0.110, 0.118, 0.129),             # #1c1e21 anomalies
}
DEFAULT_COLOUR = "deterministic"

_doc: pymupdf.Document | None = None
_lock = threading.Lock()


def _document() -> pymupdf.Document:
    global _doc
    if _doc is None:
        if not PDF.exists():
            raise FileNotFoundError(f"source PDF not found at {PDF}")
        _doc = pymupdf.open(PDF)
    return _doc


def page_count() -> int:
    with _lock:
        return _document().page_count


def render_crop(
    page: int,
    bbox: tuple[float, float, float, float] | list[float],
    *,
    zoom: float = 3.0,
    margin: float = 20.0,
    colour: str = DEFAULT_COLOUR,
    draw_box: bool = True,
) -> bytes:
    """PNG bytes for `bbox` on 1-based `page`, with the box drawn and a little
    surrounding ink for context.

    Raises IndexError for a page outside the document and ValueError for a
    bbox that does not intersect the page.
    """
    with _lock:
        doc = _document()
        if page < 1 or page > doc.page_count:
            raise IndexError(f"page {page} outside 1..{doc.page_count}")
        one = pymupdf.open()
        try:
            one.insert_pdf(doc, from_page=page - 1, to_page=page - 1)
            p = one[0]
            rect = pymupdf.Rect(*bbox)
            if rect.is_empty or rect.is_infinite:
                raise ValueError(f"degenerate bbox {list(bbox)}")
            if not (rect & p.rect).is_valid or (rect & p.rect).is_empty:
                raise ValueError(f"bbox {list(bbox)} does not intersect page {page}")

            if draw_box:
                rgb = BOX_COLOURS.get(colour, BOX_COLOURS[DEFAULT_COLOUR])
                p.draw_rect(rect, color=rgb, fill=rgb, width=1.1,
                            stroke_opacity=0.95, fill_opacity=0.10)

            clip = (rect + (-margin, -margin, margin, margin)) & p.rect
            pix = p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
            return pix.tobytes("png")
        finally:
            one.close()
