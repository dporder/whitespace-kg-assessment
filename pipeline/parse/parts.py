"""Part boundary detection, derived from the PDF alone.

The pack is an assembly of separately versioned templates bound into one file,
and each template announces itself in its own running furniture: a title in the
header and a Model Version in the footer. A boundary is a change in that
signature. Nothing here reads the embedded outline or the notes' page map;
those are stage 8 cross-check inputs and touching them in stages 0 to 2 is a
spec violation.

Two behaviours are worth stating because they change the derived count.

A page whose furniture carries no title continues the part it follows, rather
than opening a nameless one. Pages 461, 463 and 466 have no running header at
all, and pages 462, 464, 465, 467, 469 and 470 have body prose sitting high
enough to be mistaken for one; requiring a repeated furniture title keeps all of
them inside the part they belong to.

A change of Model Version under an unchanged title is recorded as an anomaly on
the part, not as a boundary. Joint Schedule 2 prints v3.1, v3.0 and v3.1 on its
three consecutive pages, which is a versioning inconsistency in the pack, not
three parts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_PAREN = re.compile(r"\s*\([^)]*\)\s*$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")

_FAMILY_RULES = (
    ("core", re.compile(r"^core terms\b", re.IGNORECASE)),
    ("award-form", re.compile(r"\baward form\b", re.IGNORECASE)),
    ("framework-schedule", re.compile(r"^framework schedule\b", re.IGNORECASE)),
    ("joint-schedule", re.compile(r"^joint schedule\b", re.IGNORECASE)),
    ("call-off-schedule", re.compile(r"^(rm\d+\s+)?call-off schedule\b", re.IGNORECASE)),
)


def slugify(title: str) -> str:
    """Deterministic id from the part's own printed title.

    The parenthetical subtitle is dropped because it names the schedule's
    subject, not the schedule: "Joint Schedule 1 (Definitions)" and
    "Call-Off Schedule 9 (Security)" are joint-schedule-1 and
    call-off-schedule-9, which is exactly how config.BATCHES names them.
    """
    base = _PAREN.sub("", title.strip())
    base = base.rstrip(":").strip()
    slug = _NON_SLUG.sub("-", base.lower()).strip("-")
    return slug or "part"


def family_for(title: str) -> Optional[str]:
    for family, pattern in _FAMILY_RULES:
        if pattern.search(title.strip()):
            return family
    return None


@dataclass
class PartRun:
    page_start: int
    page_end: int
    title: str
    slug: str
    family: Optional[str]
    model_version_raw: Optional[str] = None
    header_version_raw: Optional[str] = None
    project_version_raw: Optional[str] = None
    version_source: Optional[str] = None
    anomalies: list[str] = field(default_factory=list)


def detect_parts(page_signatures: list[tuple[int, Optional[str], Optional[str], Optional[str], Optional[str]]]) -> list[PartRun]:
    """Group pages into parts.

    `page_signatures` is one tuple per page in ascending page order:
    (page, header_title, model_version_raw, header_version_raw, project_version_raw).
    """
    runs: list[PartRun] = []
    for page, title, model_v, header_v, project_v in page_signatures:
        if title is None and runs:
            runs[-1].page_end = page
            _absorb_versions(runs[-1], page, model_v, header_v, project_v)
            continue
        if title is None:
            # No part has opened yet and this page names none: hold it in a
            # placeholder that the first titled page will rename.
            title = ""
        if runs and runs[-1].title == title:
            runs[-1].page_end = page
            _absorb_versions(runs[-1], page, model_v, header_v, project_v)
            continue
        if runs and runs[-1].title == "":
            runs[-1].title = title
            runs[-1].slug = slugify(title)
            runs[-1].family = family_for(title)
            runs[-1].page_end = page
            _absorb_versions(runs[-1], page, model_v, header_v, project_v)
            continue
        run = PartRun(
            page_start=page,
            page_end=page,
            title=title,
            slug=slugify(title) if title else "part",
            family=family_for(title) if title else None,
        )
        runs.append(run)
        _absorb_versions(run, page, model_v, header_v, project_v)
    _note_missing_title_numbers(runs)
    return _deduplicate_slugs(runs)


def _absorb_versions(
    run: PartRun,
    page: int,
    model_v: Optional[str],
    header_v: Optional[str],
    project_v: Optional[str],
) -> None:
    for raw, attr, source in (
        (model_v, "model_version_raw", "footer"),
        (header_v, "header_version_raw", "header"),
        (project_v, "project_version_raw", None),
    ):
        if raw is None:
            continue
        current = getattr(run, attr)
        if current is None:
            setattr(run, attr, raw)
            if source and run.version_source is None:
                run.version_source = source
        elif current != raw:
            note = f"template_version_varies_within_part: {current!r} then {raw!r} on page {page}"
            if note not in run.anomalies:
                run.anomalies.append(note)
    # The footer's Model Version is the template's own version; the header's
    # bare "Version:" is the fallback for parts that print no Model Version.
    if run.model_version_raw is not None:
        run.version_source = "footer"
    elif run.header_version_raw is not None:
        run.version_source = "header"


_NUMBERED_FAMILIES = ("framework-schedule", "joint-schedule", "call-off-schedule")
_TITLE_NUMBER = re.compile(r"\bSchedule\s+(\d{1,2})\b", re.IGNORECASE)


def _note_missing_title_numbers(runs: list[PartRun]) -> None:
    """A schedule that does not print its own number.

    Page 209 heads itself "Joint Schedule (Minimum Standards of Reliability)"
    where every other joint schedule prints its number. The part still parses;
    what it loses is the number a citation would use, so that is recorded.
    """
    for run in runs:
        if run.family in _NUMBERED_FAMILIES and not _TITLE_NUMBER.search(run.title):
            run.anomalies.append(
                f"part_title_missing_number: {run.title!r} names no schedule number, "
                f"so the derived id is {run.slug!r}"
            )


def _deduplicate_slugs(runs: list[PartRun]) -> list[PartRun]:
    seen: dict[str, int] = {}
    for run in runs:
        base = run.slug
        n = seen.get(base, 0)
        seen[base] = n + 1
        if n:
            run.slug = f"{base}-{n + 1}"
            run.anomalies.append(
                f"duplicate_part_title: {base!r} already used, id disambiguated to {run.slug!r}"
            )
    return runs


def canonicalise_ids(runs: list[PartRun], batches: dict) -> dict[str, str]:
    """Adopt the ids config.BATCHES declares for the parts the build addresses.

    A derived slug is renamed only when it ends with a declared batch id and the
    derived page range covers the declared range, so "framework-award-form" over
    pages 23-30 becomes "award-form" while nothing else is touched. Returns the
    map of renames applied, for the layout file to record.
    """
    renames: dict[str, str] = {}
    for batch in sorted(batches):
        spec = batches[batch]
        want = spec["part"]
        lo, hi = spec["pages"]
        for run in runs:
            if run.slug == want:
                break
            if run.page_start <= lo and run.page_end >= hi and run.slug.endswith(want):
                renames[run.slug] = want
                run.anomalies.append(
                    f"part_id_canonicalised: derived {run.slug!r} adopted config batch id {want!r}"
                )
                run.slug = want
                break
    return renames
