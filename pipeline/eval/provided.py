"""The assignment's own descriptions of the document. Stage 8 input only.

Three artifacts ship with the assignment and SPEC section 4 forbids importing
any of them into stages 0 to 7: the pipeline derives its own answer and stage 8
diffs. This module reads them at runtime, never copies them into the repo, and
records which file each one came from so the report can cite it.

- The **page map**, a markdown table of page ranges to part names. It is found
  by scanning the assignment's notes, README and brief for the first table
  whose first column is a page range, and the file it was found in is reported.
- The **embedded outline**, 498 entries, read with PyMuPDF `get_toc()` off the
  PDF at `config.PDF`.
- The notes' **stated counts** (parts, pages, outline entries), parsed from the
  prose rather than hardcoded, so if the notes change the report follows.

Nothing here is treated as ground truth. The outline is machine generated from
the source document's styling and degrades where that styling was sloppy, so a
disagreement is a question for triage, not a verdict on the parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import config

# Page-map rows look like "| 1–22 | Core terms |", with an en dash in practice.
_DASHES = "‐‑‒–—―-"
_RANGE_RE = re.compile(rf"^(\d+)\s*(?:[{_DASHES}]\s*(\d+))?$")
_ROW_RE = re.compile(r"^\|(?P<cells>.+)\|\s*$")


@dataclass
class PageMapRow:
    pages: tuple[int, int]
    name: str
    part_id: str
    row_index: int
    raw_pages: str = ""        # the cell as written, before parsing
    raw_name: str = ""         # the cell as written, before whitespace collapse

    def as_dict(self) -> dict[str, Any]:
        return {"row": self.row_index, "pages": list(self.pages),
                "name": self.name, "part_id": self.part_id,
                "raw_pages_cell": self.raw_pages, "raw_name_cell": self.raw_name}


@dataclass
class ProvidedPageMap:
    state: str = "absent"                    # loaded | absent | failed
    source_file: Optional[str] = None        # cited in the report
    rows: list[PageMapRow] = field(default_factory=list)
    stated_part_count: Optional[int] = None
    stated_page_count: Optional[int] = None
    stated_outline_entries: Optional[int] = None
    error: Optional[str] = None
    searched: list[str] = field(default_factory=list)


@dataclass
class OutlineEntry:
    index: int
    level: int
    title: str
    page: int
    label: Optional[str]        # leading number parsed off the title, "3" from "3. What..."
    stripped_title: str         # the title with that leading number removed

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "level": self.level, "title": self.title,
                "page": self.page, "label": self.label}


@dataclass
class ProvidedOutline:
    state: str = "absent"
    source_file: Optional[str] = None
    entries: list[OutlineEntry] = field(default_factory=list)
    page_count: Optional[int] = None
    error: Optional[str] = None

    def level1(self) -> list[OutlineEntry]:
        return [e for e in self.entries if e.level == 1]

    def in_pages(self, first: int, last: int) -> list[OutlineEntry]:
        return [e for e in self.entries if first <= e.page <= last]


# --------------------------------------------------------------------- part ids

_SCHEDULE_RE = re.compile(
    r"\b(framework|joint|call[\s\-]?off)\s+schedule\s*(\d+)", re.IGNORECASE)


def part_id_for(name: str) -> str:
    """Map a provided part name onto the part id convention used in config.BATCHES.

    "Call-Off Schedule 9 - Security" -> "call-off-schedule-9"
    "Joint Schedule 11 Processing Data" -> "joint-schedule-11"
    "Framework Award Form" -> "award-form"
    "Core terms" -> "core-terms"
    Anything else falls back to a slug of the whole name, which keeps the row
    in the report as an unmatched row rather than dropping it.
    """
    m = _SCHEDULE_RE.search(name)
    if m:
        family = re.sub(r"[\s\-]+", "-", m.group(1).strip().lower())
        return f"{family}-schedule-{int(m.group(2))}"
    low = name.lower()
    if "award form" in low:
        return "award-form"
    if "core term" in low:
        return "core-terms"
    slug = re.sub(r"[^a-z0-9]+", "-", low).strip("-")
    return slug or "unnamed"


# ------------------------------------------------------------------- page map

def _parse_range(cell: str) -> Optional[tuple[int, int]]:
    m = _RANGE_RE.match(cell.strip())
    if not m:
        return None
    first = int(m.group(1))
    last = int(m.group(2)) if m.group(2) else first
    return (first, last)


def _table_rows(text: str) -> list[tuple[str, str]]:
    """Every markdown row whose first cell is a page range, in file order."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group("cells").split("|")]
        if len(cells) < 2:
            continue
        if _parse_range(cells[0]) is None:
            continue
        out.append((cells[0], cells[1]))
    return out


def _stated_counts(text: str) -> tuple[Optional[int], Optional[int], Optional[int]]:
    def find(pattern: str) -> Optional[int]:
        m = re.search(pattern, text, re.IGNORECASE)
        return int(m.group(1).replace(",", "")) if m else None

    parts = find(r"(\d[\d,]*)\s+constituent\s+parts")
    pages = find(r"\*{0,2}(\d[\d,]*)\s*\*{0,2}\s+pages\b")
    entries = find(r"outline[^.]{0,80}?\((\d[\d,]*)\s+entries\)")
    return parts, pages, entries


def notes_candidates() -> list[Path]:
    """Where the provided page map might live, most likely first."""
    doc_dir = config.PDF.parent
    root = doc_dir.parent
    return [doc_dir / "DOCUMENT_NOTES.md", root / "README.md", root / "BRIEF.md"]


def load_page_map(candidates: Optional[list[Path]] = None) -> ProvidedPageMap:
    """Find and parse the provided page map, citing the file it came from."""
    out = ProvidedPageMap()
    paths = candidates if candidates is not None else notes_candidates()
    out.searched = [str(p) for p in paths]
    for path in paths:
        if not path.exists():
            continue
        try:
            text = path.read_text()
        except Exception as exc:                          # noqa: BLE001
            out.state, out.error = "failed", f"{type(exc).__name__}: {exc}"
            return out
        rows = _table_rows(text)
        if not rows:
            continue
        parsed: list[PageMapRow] = []
        for i, (pages_cell, name_cell) in enumerate(rows):
            rng = _parse_range(pages_cell)
            if rng is None:
                continue
            # The raw cells are kept beside the parsed ones. "Joint Schedule 7  -
            # Financial Difficulties" has a double space and "Joint Schedule 11
            # Processing Data" has no dash at all; those are facts about the
            # provided artifact, and a report that only ever shows the tidied
            # form cannot be used to argue about the artifact itself.
            name = re.sub(r"\s+", " ", name_cell).strip()
            parsed.append(PageMapRow(pages=rng, name=name,
                                     part_id=part_id_for(name), row_index=i,
                                     raw_pages=pages_cell, raw_name=name_cell))
        out.state = "loaded"
        out.source_file = str(path)
        out.rows = parsed
        out.stated_part_count, out.stated_page_count, out.stated_outline_entries = \
            _stated_counts(text)
        return out
    out.error = "no markdown table with a page-range first column in any candidate file"
    return out


# -------------------------------------------------------------------- outline

_LABEL_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]?\s+(.*)$")
_PART_LABEL_RE = re.compile(r"^\s*(PART\s+\d+[A-Z]?|ANNEX\s+\d+|SCHEDULE\s+\d+)\s*[:.\-]?\s*(.*)$",
                            re.IGNORECASE)


def split_label(title: str) -> tuple[Optional[str], str]:
    """Split a leading number off an outline title: '3. What...' -> ('3', 'What...')."""
    m = _LABEL_RE.match(title)
    if m:
        return m.group(1), m.group(2).strip()
    m = _PART_LABEL_RE.match(title)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip(), m.group(2).strip()
    return None, title.strip()


def load_outline(pdf: Optional[Path] = None) -> ProvidedOutline:
    """The PDF's embedded outline via PyMuPDF get_toc(). Read only, never copied."""
    out = ProvidedOutline()
    path = pdf if pdf is not None else config.PDF
    out.source_file = str(path)
    if not path.exists():
        out.error = "PDF not found at config.PDF"
        return out
    try:
        import pymupdf                                    # noqa: PLC0415 - optional at import time
    except Exception as exc:                              # noqa: BLE001
        out.state, out.error = "failed", f"PyMuPDF unavailable: {exc}"
        return out
    try:
        with pymupdf.open(path) as doc:
            out.page_count = doc.page_count
            toc = doc.get_toc()
    except Exception as exc:                              # noqa: BLE001
        out.state, out.error = "failed", f"{type(exc).__name__}: {exc}"
        return out
    for i, row in enumerate(toc):
        level, title, page = row[0], row[1], row[2]
        label, stripped = split_label(title)
        out.entries.append(OutlineEntry(index=i, level=int(level), title=title,
                                        page=int(page), label=label,
                                        stripped_title=stripped))
    out.state = "loaded"
    return out
