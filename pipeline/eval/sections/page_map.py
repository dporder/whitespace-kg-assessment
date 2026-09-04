"""`page_map_vs_provided`: the derived page map against the assignment's.

SPEC 2.6: "the derived page map diffed against the one in the assignment notes,
per part agreement, plus the derived part count against their stated 46 and
their table's 48 rows."

The derived map comes from stage 1 layout output where that exists, and falls
back to the page ranges on the stage 2 part nodes, which are schema-guaranteed.
Which source was used is printed, because a page map derived from a tree is
evidence about the tree, not about the extractor.

Neither map is treated as ground truth. The notes say 46 constituent parts and
their own table lists 48 rows; the report prints both, prints the embedded
outline's top-level count as a third independent witness, and prints what the
document supports as far as this run derived it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pipeline.eval.context import Context, LIST_CAP
from pipeline.eval.rates import MEASURED, NO_DATA, PARTIAL, Rate, Section, cap


def _pages_from_layout(payload: Any) -> Optional[tuple[int, int]]:
    """Tolerant page extraction from a stage 1 layout file.

    Stage 1's schema is the parser-builder's to define and does not exist yet,
    so this recognises the obvious shapes and returns None rather than guessing
    when it sees something else.
    """
    if isinstance(payload, dict):
        if isinstance(payload.get("page_start"), int) and isinstance(payload.get("page_end"), int):
            return (payload["page_start"], payload["page_end"])
        pages = payload.get("pages")
        if isinstance(pages, list) and pages:
            nums: list[int] = []
            for p in pages:
                if isinstance(p, int):
                    nums.append(p)
                elif isinstance(p, dict):
                    for key in ("page", "number", "page_number", "index"):
                        if isinstance(p.get(key), int):
                            nums.append(p[key])
                            break
            if nums:
                return (min(nums), max(nums))
        blocks = payload.get("blocks")
        if isinstance(blocks, list) and blocks:
            nums = [b["page"] for b in blocks
                    if isinstance(b, dict) and isinstance(b.get("page"), int)]
            if nums:
                return (min(nums), max(nums))
    return None


def derive(ctx: Context) -> tuple[dict[str, tuple[int, int]], str, list[str]]:
    """Derived page map, its provenance, and per-part notes."""
    notes: list[str] = []
    layout_dir = ctx.inputs.root / "layout"
    derived: dict[str, tuple[int, int]] = {}
    used_layout = False
    if layout_dir.is_dir():
        for part in ctx.inputs.scope_parts:
            path = layout_dir / f"{part}.json"
            if not path.exists():
                continue
            try:
                rng = _pages_from_layout(json.loads(path.read_text()))
            except Exception as exc:                      # noqa: BLE001
                notes.append(f"{part}: stage 1 layout failed to parse ({type(exc).__name__})")
                continue
            if rng is None:
                notes.append(f"{part}: stage 1 layout present but no recognisable page field")
                continue
            derived[part] = rng
            used_layout = True

    for part, tree in sorted(ctx.inputs.trees.items()):
        if part in derived:
            continue
        derived[part] = (tree.page_start, tree.page_end)

    if used_layout and len(derived) > len([p for p in derived if p]):
        source = "stage 1 layout"
    elif used_layout:
        source = "stage 1 layout, falling back to stage 2 part page ranges"
    else:
        source = "stage 2 part page ranges (stage 1 layout absent or unreadable)"
    return derived, source, notes


def build(ctx: Context) -> Section:
    s = Section("page_map_vs_provided")
    pm = ctx.page_map
    derived, derived_source, notes = derive(ctx)

    s.data["derived_from"] = derived_source
    s.data["derived_notes"] = notes
    s.data["provided"] = {
        "state": pm.state,
        "source_file": pm.source_file,
        "searched": pm.searched,
        "error": pm.error,
        "table_rows": len(pm.rows),
        "stated_part_count": pm.stated_part_count,
        "stated_page_count": pm.stated_page_count,
        "stated_outline_entries": pm.stated_outline_entries,
    }

    if pm.state != "loaded":
        s.status = NO_DATA
        s.reason = pm.error or "provided page map not found"
        s.line(f"_{s.reason}. Searched: {', '.join(pm.searched)}._")
        return s

    provided_by_id = {r.part_id: r for r in pm.rows}
    rows: list[dict[str, Any]] = []
    agree = 0
    for part in sorted(derived):
        d_first, d_last = derived[part]
        row = provided_by_id.get(part)
        alignment = "part id"
        if row is None:
            overlapping = [r for r in pm.rows
                           if not (r.pages[1] < d_first or r.pages[0] > d_last)]
            if len(overlapping) == 1:
                row, alignment = overlapping[0], "page overlap"
        if row is None:
            rows.append({"part": part, "derived": [d_first, d_last], "provided": None,
                         "aligned_by": None, "agreement": "no provided row aligns"})
            continue
        same = (row.pages[0] == d_first and row.pages[1] == d_last)
        agree += 1 if same else 0
        rows.append({
            "part": part, "derived": [d_first, d_last], "provided": list(row.pages),
            "provided_name": row.name, "aligned_by": alignment,
            "agreement": "exact" if same else "boundaries differ",
            "first_page_delta": d_first - row.pages[0],
            "last_page_delta": d_last - row.pages[1],
        })

    unmatched_rows = [r.as_dict() for r in pm.rows
                      if r.part_id not in derived and r.part_id not in
                      {x["part"] for x in rows}]
    per_part = Rate(agree, len([r for r in rows if r["provided"] is not None]))

    outline_level1 = len(ctx.outline.level1()) if ctx.outline.state == "loaded" else None
    derived_pages = sorted({p for rng in derived.values() for p in range(rng[0], rng[1] + 1)})
    covers_document = bool(pm.stated_page_count) and \
        len(derived_pages) >= pm.stated_page_count
    counts = {
        "derived_parts_in_this_run": len(derived),
        "notes_stated_part_count": pm.stated_part_count,
        "notes_table_rows": len(pm.rows),
        "embedded_outline_top_level_entries": outline_level1,
        "derived_count_comparable": covers_document,
        "not_comparable_reason": None if covers_document else
        (f"this run derived {len(derived)} part(s) covering {len(derived_pages)} page(s); "
         f"the provided counts describe all "
         f"{pm.stated_page_count if pm.stated_page_count else 'unknown'} pages"),
    }
    s.data["per_part"] = rows
    s.data["per_part_agreement"] = per_part.as_dict()
    s.data["provided_rows_with_no_derived_part"] = cap(unmatched_rows, LIST_CAP)[0]
    s.data["provided_rows_with_no_derived_part_not_listed"] = cap(unmatched_rows, LIST_CAP)[1]
    s.data["part_counts"] = counts

    s.status = MEASURED if covers_document else PARTIAL
    s.reason = counts["not_comparable_reason"]

    s.line(f"Provided map read from `{pm.source_file}` "
           f"({len(pm.rows)} rows). Derived map from {derived_source}.")
    if ctx.inputs.source == "fixtures":
        s.line("_Fixture page numbers are fixture-local by construction "
               "(fixtures/README.md), so disagreement below is expected and says "
               "nothing about the parser. The diff itself is what is being "
               "demonstrated._")
    s.line()
    s.line(f"Per-part exact agreement: **{per_part}**")
    s.line()
    s.table(["part", "derived pages", "provided pages", "provided name", "aligned by",
             "agreement", "first Δ", "last Δ"],
            [[r["part"],
              f'{r["derived"][0]}-{r["derived"][1]}',
              f'{r["provided"][0]}-{r["provided"][1]}' if r["provided"] else "—",
              r.get("provided_name", "—"), r.get("aligned_by") or "—", r["agreement"],
              r.get("first_page_delta"), r.get("last_page_delta")] for r in rows])
    s.line()
    s.line("**Part count, three witnesses and what this run derived**")
    s.table(["source", "count"],
            [["notes prose, stated constituent parts", counts["notes_stated_part_count"]],
             ["notes page-map table rows", counts["notes_table_rows"]],
             ["PDF embedded outline, top-level entries",
              counts["embedded_outline_top_level_entries"]],
             ["derived by this run", counts["derived_parts_in_this_run"]]])
    if not covers_document:
        s.bullet(f"derived count is **not** comparable yet: {counts['not_comparable_reason']}")
    if unmatched_rows:
        s.bullet(f"{len(unmatched_rows)} provided row(s) have no derived part in this run "
                 f"(expected while the run is scoped to a batch)")
    for n in notes:
        s.bullet(n)
    return s
