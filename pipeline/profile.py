"""Stage 0: profile the document, assign a rulebook, and check the shoe fits.

    python -m pipeline.profile
    python -m pipeline.profile --pages 1 22

Writes `output/profile.json` always. On any alarm it also writes
`output/quarantine.json` naming the signal with examples and page numbers, and
exits 2 having loaded nothing. There is deliberately no parse-anyway fallback:
a confidently wrong hierarchy corrupts every citation built on top of it.

The five checks are SPEC 2.7's, with thresholds from
`config.QUARANTINE_THRESHOLDS`.

1. No interpretation clause, or one naming units the rulebook has never heard
   of. Wrong rulebook.
2. Too much numbering the rulebook's grammar does not cover.
3. Too much homeless text: what share of the body attached to no node.
4. Depth outside the rulebook's range.
5. Indentation disagreeing with numbering.

Check 5 abstains where it has nothing to measure, and says so rather than
passing quietly. Core Terms sets its top-level headings at x=30.4, its clauses
at x=27.0 and its subclauses at x=26.4, so within four points the dotted levels
carry no indentation signal at all; a check that pretended otherwise would fire
on all 146 clauses of a perfectly good parse. Where a part does indent its
levels, as Call-Off Schedule 9 does at 72, 86 and 119, the check runs.

The outline is reported as a flag only. Its 498 entries are a stage 8
cross-check input and reading them here would be a spec violation.
"""
from __future__ import annotations

import argparse
import collections
import math
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from pipeline.assemble.tree import build_part
from pipeline.parse.blocks import PageInput, build_blocks, collect_page, modal_size
from pipeline.parse.document import DocumentScan, scan
from pipeline.parse.geometry import INDENT_TOLERANCE, MIN_INDENT_STEP, median
from pipeline.parse.layout import build_layout
from pipeline.parse.model import dump_json
from pipeline.parse.numbering import Rulebook
from pipeline.parse.words import font_size_histogram

# Unit words a document may name in its interpretation clause. A word here that
# the assigned rulebook does not declare is check 1 firing.
UNIT_WORDS = (
    "Clause", "Clauses", "Schedule", "Schedules", "Part", "Parts",
    "Paragraph", "Paragraphs", "Annex", "Annexes", "Table", "Tables",
    "Article", "Articles", "Section", "Sections", "Recital", "Recitals",
    "Regulation", "Regulations", "Rule", "Rules", "Item", "Items",
)
_QUOTED_UNIT = re.compile(r"[\"“]([A-Z][a-z]+)[\"”]")

# All five SPEC 2.7 checks run per part, not just the two that used to.
CHECK_IDS = (
    "interpretation_clause",
    "unmatched_numbering",
    "orphan_text",
    "depth_out_of_range",
    "geometry_disagrees_with_numbering",
)
# A dotted number at the start of a line, however deep.
_DOTTED_NUMBER = re.compile(r"^\s{0,12}(\d{1,3}(?:\.\d{1,3})+)\.?\s")


def _singular(word: str) -> str:
    if word.endswith("es") and word[:-2] in ("Annex", "Class"):
        return word[:-2]
    return word[:-1] if word.endswith("s") else word


def profile_pages(document: DocumentScan) -> list[dict]:
    out = []
    for page_no in sorted(document.pages):
        page = document.pages[page_no]
        out.append(
            {
                "page": page_no,
                "width": round(page.width, 2),
                "height": round(page.height, 2),
                "has_text_layer": page.has_text_layer,
                "chars": sum(len(l.text) for l in page.lines),
                "body_chars": page.body_chars,
                "furniture_lines": len(page.furniture.stripped),
                "printed_page": page.furniture.printed_page,
                "images": page.n_images,
                "image_area_fraction": round(page.image_area, 4),
                "drawings": page.n_drawings,
                "font_sizes": font_size_histogram(page.lines),
                "route": "text_layer" if page.has_text_layer else "layout_ocr",
            }
        )
    return out


def assign_rulebook(document: DocumentScan, profiles: dict) -> tuple[str, dict]:
    """Score every rulebook in config and take the best.

    Only one family ships today, and the machinery still scores rather than
    assumes, because the point of the design is that a new family is a config
    entry rather than a parser change.
    """
    body = [
        line
        for page in document.pages.values()
        for line in page.furniture.body
        if line.text.strip()
    ]
    scores: dict[str, dict] = {}
    for name in sorted(profiles):
        rulebook = Rulebook(name, profiles[name])
        matched = sum(1 for l in body if rulebook.match(l.text))
        numbered = sum(1 for l in body if rulebook.looks_numbered(l.text))
        cues = sum(
            1 for l in body for cue in rulebook.interpretation_cues if cue.search(l.text)
        )
        scores[name] = {
            "lines_matching_grammar": matched,
            "lines_looking_numbered": numbered,
            "interpretation_cue_hits": cues,
            "coverage": round(matched / numbered, 4) if numbered else 0.0,
        }
    best = sorted(
        scores,
        key=lambda n: (
            -scores[n]["coverage"],
            -scores[n]["lines_matching_grammar"],
            0 if n == config.DEFAULT_PROFILE else 1,
            n,
        ),
    )[0]
    return best, scores


def find_interpretation(document: DocumentScan, rulebook: Rulebook) -> dict:
    """Check 1: locate the interpretation clause and the units it names."""
    hits: list[dict] = []
    named: dict[str, list[int]] = {}
    for page_no in sorted(document.pages):
        for line in document.pages[page_no].furniture.body:
            text = line.text
            for cue in rulebook.interpretation_cues:
                if cue.search(text):
                    if len(hits) < 12:
                        hits.append({"page": page_no, "cue": cue.pattern, "text": text.strip()[:220]})
                    for quoted in _QUOTED_UNIT.findall(text):
                        singular = _singular(quoted)
                        if singular in (_singular(w) for w in UNIT_WORDS):
                            named.setdefault(singular, []).append(page_no)
                    break
    known = {_singular(w) for w in rulebook.units_from_document}
    unknown = sorted(u for u in named if u not in known)
    return {
        "found": bool(hits),
        "cue_hits": len(hits),
        "examples": hits,
        "units_named_by_document": {k: sorted(set(v))[:5] for k, v in sorted(named.items())},
        "units_unknown_to_rulebook": unknown,
    }


def numbering_coverage(
    document: DocumentScan, rulebook: Rulebook, pdf_path: Path
) -> dict:
    """Check 2: numbering the rulebook's grammar does not cover.

    Measured on the visual lines the parser actually consumes, not on the raw
    lines the text layer emits. The pack routinely sets a number in a narrow
    left column with its sentence beside it — "1." at x=72 and "In this
    Schedule, ..." at x=107 on the same baseline — and the parser merges those
    into one line before the grammar ever sees them. Scoring the grammar
    against the unmerged halves measures the PDF's column layout rather than
    the rulebook: it reported 217 uncovered lines where the parser had covered
    all but 64 of them.

    Lines inside a ruled grid are excluded for the same reason: a form row
    printed "3. rFramework" and a table's row counters are numbering of a
    table, not of the provision ladder.
    """
    import pymupdf
    from pipeline.parse.tables import page_grids
    from pipeline.parse.words import merge_visual_lines

    doc = pymupdf.open(pdf_path)
    total = 0
    count = 0
    unmatched: list[dict] = []
    by_part: dict[str, int] = {}
    numbered_by_part: dict[str, int] = {}
    examples_by_part: dict[str, list[dict]] = {}
    part_of = {
        page: part.slug
        for part in document.parts
        for page in range(part.page_start, part.page_end + 1)
    }
    for page_no in sorted(document.pages):
        grids = page_grids(doc[page_no - 1], page_no)
        body = document.pages[page_no].furniture.body
        in_grid = {
            id(line)
            for grid in grids
            for line in body
            if grid.locate(line.bbox) is not None
        }
        free = [l for l in body if id(l) not in in_grid]
        for line in merge_visual_lines(free, page_no):
            text = line.text
            if not rulebook.looks_numbered(text):
                continue
            total += 1
            part = part_of.get(page_no, "?")
            numbered_by_part[part] = numbered_by_part.get(part, 0) + 1
            if rulebook.match(text) is None:
                count += 1
                by_part[part] = by_part.get(part, 0) + 1
                if len(unmatched) < 40:
                    unmatched.append({"page": page_no, "part": part, "text": text.strip()[:150]})
                # Every failing part keeps its own examples, so a further
                # rulebook entry can be written against evidence rather than
                # guessed at from a document-wide sample.
                bucket = examples_by_part.setdefault(part, [])
                if len(bucket) < 6:
                    bucket.append(
                        {
                            "page": page_no,
                            "style": _numbering_style(text),
                            "text": text.strip()[:120],
                        }
                    )
    doc.close()
    return {
        "numbered_lines": total,
        "unmatched_lines": count,
        "rate": round(count / total, 4) if total else 0.0,
        "numbered_by_part": dict(sorted(numbered_by_part.items())),
        "unmatched_by_part": dict(sorted(by_part.items(), key=lambda kv: (-kv[1], kv[0]))),
        "examples": unmatched,
        "examples_by_part": {k: examples_by_part[k] for k in sorted(examples_by_part)},
        "styles_by_part": {
            part: dict(sorted(collections.Counter(e["style"] for e in items).items()))
            for part, items in sorted(examples_by_part.items())
        },
    }


# Shapes of numbering the rulebook did not cover, named so a residue can be
# read as a style rather than as a list of lines.
_STYLE_PATTERNS = (
    ("dotted_with_trailing_period", re.compile(r"^\s*\d{1,3}(?:\.\d{1,3})+\.\s")),
    ("four_or_more_dotted_levels", re.compile(r"^\s*\d{1,3}(?:\.\d{1,3}){3,}")),
    ("bare_integer_with_period", re.compile(r"^\s*\d{1,3}\.\s")),
    ("bare_integer_with_bracket", re.compile(r"^\s*\d{1,3}\)\s")),
    ("letter_with_period", re.compile(r"^\s*[a-zA-Z]{1,2}\.\s")),
    ("letter_with_bracket", re.compile(r"^\s*\(?[a-zA-Z]{1,3}\)\s")),
    ("roman_with_period", re.compile(r"^\s*[ivxlIVXL]{1,6}\.\s")),
    ("numbering_token_alone_on_its_line", re.compile(r"^\s*\S{1,8}\s*$")),
)


# An all-uppercase token in brackets at the start of a line is an abbreviation
# introduced mid-sentence, not a numbering token: "(DBS) or otherwise), is
# employed ..." opens a wrapped line on page 432. The rulebook can never match
# it, so it inflates the unmatched count without being a grammar gap.
_ABBREVIATION_OPENER = re.compile(r"^\s*\([A-Z]{2,6}\)")


def _looks_like_detector_artifact(text: str) -> bool:
    return bool(_ABBREVIATION_OPENER.match(text))


def _ink(text: str) -> int:
    """Non-whitespace characters. The one basis on which a reflowed block and
    the source lines it came from can be compared: reflow moves whitespace, it
    never adds or removes a visible glyph."""
    return sum(1 for ch in text if not ch.isspace())


def _numbering_style(text: str) -> str:
    for name, pattern in _STYLE_PATTERNS:
        if pattern.match(text):
            return name
    return "other"


def probe_parts(pdf_path: Path, document: DocumentScan, rulebook: Rulebook) -> dict:
    """Checks 3, 4 and 5, which need a tree to measure against.

    Stage 0 runs the same deterministic machinery stages 1 and 2 use rather
    than a second approximation of it, so what it reports is what the pipeline
    would actually produce.
    """
    import pymupdf

    doc = pymupdf.open(pdf_path)
    orphan_chars = 0
    body_chars = 0
    max_dotted = 0
    max_tree_depth = 0
    deep_examples: list[dict] = []
    geometry = {"tested": 0, "disagreements": 0, "abstained_parts": [], "measured_parts": []}
    disagreement_examples: list[dict] = []
    orphan_examples: list[dict] = []
    # Per-part COUNTS are uncapped; the caps below apply only to stored
    # examples. Counting capped examples attributed every disagreement past
    # the cap to whichever part happened to hit it first.
    orphan_by_part: dict[str, int] = {}
    part_ink_by_part: dict[str, int] = {}
    geometry_by_part: dict[str, int] = {}
    geometry_tested_by_part: dict[str, int] = {}
    deep_by_part: dict[str, list] = {}

    for part in document.parts:
        inputs: list[PageInput] = []
        for page_no in range(part.page_start, part.page_end + 1):
            page_scan = document.pages[page_no]
            inputs.append(collect_page(doc[page_no - 1], page_no, page_scan.furniture.body))
        blocks = build_blocks(inputs, rulebook)

        # Both sides counted in ink: non-whitespace characters only. Reflow
        # moves whitespace around, joining wrapped lines with a space that was
        # never a character on the page, so counting raw length compared a
        # reflowed string against an unreflowed one and made `placed` exceed the
        # body it came from by up to 974 characters. Every visible glyph in a
        # block came from the page, so ink is the one basis both sides share.
        #
        # A numbered block's own words exclude its number, which moves to the
        # node's label, so the numbering token counts as placed too.
        placed = sum(
            _ink(b.text) + _ink(b.number_printed or "")
            for b in blocks
            if b.block_kind in ("numbered", "prose", "part_title")
        )
        placed += sum(_ink(c.text) for b in blocks for c in b.cells)
        part_body = sum(
            _ink(line.text)
            for p in range(part.page_start, part.page_end + 1)
            for line in document.pages[p].furniture.body
        )
        # Signed, not clamped. A surplus is as suspicious as a deficit: it means
        # ink was counted twice, which a max(0, ...) would hide completely and
        # with it any chance of this check ever firing.
        residual = part_body - placed
        body_chars += part_body
        orphan_chars += residual
        orphan_by_part[part.slug] = residual
        part_ink_by_part[part.slug] = part_body
        if residual and len(orphan_examples) < 15:
            orphan_examples.append(
                {
                    "part": part.slug,
                    "pages": [part.page_start, part.page_end],
                    "body_ink": part_body,
                    "placed_ink": placed,
                    "residual": residual,
                }
            )

        # Depth is measured on the numbering the page prints, not on the
        # numbering the rulebook happened to match. Measuring the matches only
        # makes this check structurally unable to fail: a number too deep for
        # the rulebook does not match it, so it would never be counted, and
        # check 4 would report the rulebook's own ceiling back to itself. The
        # pack really does carry 46 four-level numbers (2.1.1.1, 9.1.3.2,
        # 4.1.2.1.), which is what this catches.
        for page_no in range(part.page_start, part.page_end + 1):
            for line in document.pages[page_no].furniture.body:
                m = _DOTTED_NUMBER.match(line.text)
                if not m:
                    continue
                dots = m.group(1).count(".") + 1
                if dots > rulebook.max_dotted_depth:
                    # Per-part ledger, uncapped and never reset. The shared
                    # `deep_examples` list below is a display sample that resets
                    # whenever a deeper number turns up; a part's own record of
                    # numbering too deep for the rulebook must not depend on
                    # what some other part printed.
                    deep_by_part.setdefault(part.slug, []).append(
                        {"part": part.slug, "page": page_no, "number": m.group(1),
                         "text": line.text.strip()[:90]}
                    )
                if dots > max_dotted:
                    max_dotted = dots
                    deep_examples = [
                        {"part": part.slug, "page": page_no, "number": m.group(1),
                         "text": line.text.strip()[:90]}
                    ]
                elif dots == max_dotted and len(deep_examples) < 6:
                    deep_examples.append(
                        {"part": part.slug, "page": page_no, "number": m.group(1),
                         "text": line.text.strip()[:90]}
                    )
        for block in blocks:
            max_tree_depth = max(max_tree_depth, block.depth or 0)

        _measure_geometry(
            part.slug, blocks, geometry, disagreement_examples,
            geometry_by_part, geometry_tested_by_part,
        )

    doc.close()
    return {
        "orphan": {
            "body_ink": body_chars,
            "residual_ink": orphan_chars,
            "rate": round(abs(orphan_chars) / body_chars, 4) if body_chars else 0.0,
            "signed_rate": round(orphan_chars / body_chars, 6) if body_chars else 0.0,
            "by_part": dict(sorted(orphan_by_part.items())),
            "ink_by_part": dict(sorted(part_ink_by_part.items())),
            "examples": orphan_examples,
        },
        "depth": {
            "max_dotted_depth": max_dotted,
            "rulebook_max_dotted_depth": rulebook.max_dotted_depth,
            "max_numbered_depth": max_tree_depth,
            "rulebook_levels": len(rulebook.numbered_levels),
            "by_part": {k: v for k, v in sorted(deep_by_part.items())},
            "examples": deep_examples,
        },
        "geometry": {
            "by_part": dict(sorted(geometry_by_part.items())),
            "tested_by_part": dict(sorted(geometry_tested_by_part.items())),
            "pairs_tested": geometry["tested"],
            "disagreements": geometry["disagreements"],
            "rate": round(geometry["disagreements"] / geometry["tested"], 4)
            if geometry["tested"]
            else 0.0,
            "parts_measured": geometry["measured_parts"],
            "parts_abstained": geometry["abstained_parts"],
            "examples": disagreement_examples,
        },
    }


def _measure_geometry(
    part_id: str,
    blocks: list,
    geometry: dict,
    examples: list[dict],
    by_part: dict[str, int],
    tested_by_part: dict[str, int],
) -> None:
    """Check 5, per part, abstaining where indentation carries no signal."""
    lefts: dict[int, list[float]] = {}
    for block in blocks:
        if block.block_kind == "numbered" and block.depth and block.left is not None:
            lefts.setdefault(block.depth, []).append(block.left)
    depths = sorted(lefts)
    if len(depths) < 2:
        geometry["abstained_parts"].append({"part": part_id, "reason": "fewer than two numbered depths"})
        return
    medians = {d: median(lefts[d]) for d in depths}

    # Abstain per level pair, not per part. Core Terms sets its headings at
    # x=30.4, its clauses at 27.0 and its subclauses at 26.4, so heading to
    # clause and clause to subclause carry no indentation signal at all, while
    # subclause to lettered item steps 29 points and does. Measuring only the
    # pairs that separate is the difference between reporting one real signal
    # and reporting 146 false ones.
    measurable = {
        d
        for d in depths
        if d - 1 in medians and medians[d] - medians[d - 1] >= MIN_INDENT_STEP
    }
    abstained = {
        str(d): round(medians[d] - medians[d - 1], 1)
        for d in depths
        if d - 1 in medians and d not in measurable
    }
    if not measurable:
        geometry["abstained_parts"].append(
            {
                "part": part_id,
                "reason": "no level pair is separated by indentation",
                "level_medians": {str(d): round(medians[d], 1) for d in depths},
                "steps": abstained,
            }
        )
        return
    geometry["measured_parts"].append(
        {
            "part": part_id,
            "level_medians": {str(d): round(medians[d], 1) for d in depths},
            "levels_measured": sorted(measurable),
            "level_steps_too_small_to_measure": abstained,
        }
    )
    for block in blocks:
        if block.block_kind != "numbered" or not block.depth or block.left is None:
            continue
        parent_depth = block.depth - 1
        if parent_depth not in medians or block.depth not in measurable:
            continue
        geometry["tested"] += 1
        by_part.setdefault(part_id, 0)
        tested_by_part[part_id] = tested_by_part.get(part_id, 0) + 1
        if block.left < medians[parent_depth] - INDENT_TOLERANCE:
            geometry["disagreements"] += 1
            # Uncapped per-part count. Counting the capped example list instead
            # attributed every disagreement past the twentieth to whichever
            # part happened to fill the cap.
            by_part[part_id] = by_part.get(part_id, 0) + 1
            if len(examples) < 20:
                examples.append(
                    {
                        "part": part_id,
                        "page": block.page_start,
                        "number": block.number,
                        "left": round(block.left, 1),
                        "parent_level_median_left": round(medians[parent_depth], 1),
                    }
                )


def fit_by_part(
    numbering: dict,
    probe: dict,
    document: DocumentScan,
    thresholds: dict,
    interpretation: dict,
) -> dict:
    """The same fit checks, scoped to each part.

    The pack is not one document, it is roughly fifty separately versioned
    templates bound into one file, and they do not share a numbering house
    style: Framework Schedule 1 numbers its paragraphs "1.1." with a trailing
    period, which the rulebook's clause pattern does not cover, while Core
    Terms fits it exactly. Reporting one verdict for the whole binding would
    either quarantine parts that parse cleanly or wave through parts that do
    not, so the document-level verdict from SPEC 2.7 is kept exactly as
    specified and this sits beside it, saying which parts it was that failed.
    """
    # Every ledger below is the uncapped per-part count from `probe_parts`.
    # These used to be reconstructed by counting the capped example lists, which
    # attributed everything past a cap to whichever part filled it and silently
    # zeroed the rest: Framework Schedule 1 reported 20 geometry disagreements
    # and passed.
    unmatched = numbering.get("unmatched_by_part", {})
    orphan_by_part = probe["orphan"].get("by_part", {})
    geometry_by_part = probe["geometry"].get("by_part", {})
    geometry_tested_by_part = probe["geometry"].get("tested_by_part", {})
    depth_limit = probe["depth"]["rulebook_max_dotted_depth"]
    deep_by_part = probe["depth"].get("by_part", {})
    part_ink_by_part = probe["orphan"].get("ink_by_part", {})
    interpretation_ok = interpretation["found"]
    unknown_units = interpretation["units_unknown_to_rulebook"]

    out: dict[str, dict] = {}
    limit = thresholds["max_unmatched_numbering_rate"]
    for part in document.parts:
        counts = numbering.get("numbered_by_part", {}).get(part.slug, 0)
        bad = unmatched.get(part.slug, 0)
        rate = round(bad / counts, 4) if counts else 0.0
        alarms = []
        if rate > limit:
            examples = numbering.get("examples_by_part", {}).get(part.slug, [])[:6]
            alarm = {
                "check": "unmatched_numbering",
                "detail": f"{bad} of {counts} numbered lines in this part match no "
                f"rulebook pattern, rate {rate} > {limit}",
                "examples": examples,
                "unmatched_styles": numbering.get("styles_by_part", {}).get(part.slug, {}),
            }
            suspected = [e for e in examples if _looks_like_detector_artifact(e["text"])]
            if suspected:
                # Say so where the quarantine may rest on the shape detector
                # rather than on the document. A reader of the eval report
                # should be able to see that without re-deriving it, and the
                # honest state of a small part failing on one such line is that
                # the gate held for a reason nobody has confirmed yet.
                alarm["suspected_detector_artifact"] = {
                    "note": "these lines are counted as numbering-shaped by the parser's "
                            "looks_numbered detector but are prose, most likely a bracketed "
                            "abbreviation opening a wrapped line; the detector was left "
                            "untouched rather than tuned to clear this gate",
                    "lines": [{"page": e["page"], "text": e["text"]} for e in suspected],
                }
            alarms.append(alarm)
        if part.slug in deep_by_part:
            alarms.append(
                {
                    "check": "depth_out_of_range",
                    "detail": f"this part numbers deeper than the rulebook's "
                    f"{depth_limit} dotted levels, {len(deep_by_part[part.slug])} line(s)",
                    "examples": deep_by_part[part.slug][:5],
                }
            )

        # Check 1, scoped as far as it can be. The interpretation clause is a
        # document-level fact: one clause governs the whole pack, so its absence
        # or a unit it names that the rulebook lacks condemns every part, and a
        # part is not credited with an interpretation clause of its own.
        if thresholds.get("require_interpretation_clause") and not interpretation_ok:
            alarms.append(
                {
                    "check": "interpretation_clause_missing",
                    "detail": "no interpretation cue from the rulebook appears in the document",
                    "examples": [],
                }
            )
        if unknown_units:
            alarms.append(
                {
                    "check": "interpretation_names_unknown_units",
                    "detail": "the document names units the rulebook does not declare: "
                    + ", ".join(unknown_units),
                    "examples": interpretation["examples"][:3],
                }
            )

        # Check 3, on the signed residual: a surplus means ink counted twice.
        residual = orphan_by_part.get(part.slug, 0)
        part_ink = part_ink_by_part.get(part.slug, 0)
        orphan_rate = abs(residual) / part_ink if part_ink else 0.0
        if orphan_rate > thresholds["max_orphan_block_rate"]:
            alarms.append(
                {
                    "check": "orphan_text",
                    "detail": f"{residual:+d} ink characters of {part_ink} unaccounted for "
                    f"({'deficit' if residual > 0 else 'surplus'}), rate "
                    f"{round(orphan_rate, 4)} > {thresholds['max_orphan_block_rate']}",
                    "examples": [
                        e for e in probe["orphan"].get("examples", [])
                        if e.get("part") == part.slug
                    ][:3],
                }
            )

        # Check 5, with the part's own denominator.
        geometry_bad = geometry_by_part.get(part.slug, 0)
        geometry_tested = geometry_tested_by_part.get(part.slug, 0)
        geometry_rate = geometry_bad / geometry_tested if geometry_tested else 0.0
        if geometry_tested and geometry_rate > thresholds["max_geometry_disagreement"]:
            alarms.append(
                {
                    "check": "geometry_disagrees_with_numbering",
                    "detail": f"{geometry_bad} of {geometry_tested} numbered lines sit left "
                    f"of their level's parent, rate {round(geometry_rate, 4)} > "
                    f"{thresholds['max_geometry_disagreement']}",
                    "examples": [
                        e for e in probe["geometry"].get("examples", [])
                        if e.get("part") == part.slug
                    ][:3],
                }
            )

        out[part.slug] = {
            "pages": [part.page_start, part.page_end],
            "numbered_lines": counts,
            "unmatched_lines": bad,
            "unmatched_rate": rate,
            # The residue, named as styles rather than as a list of lines, so a
            # further rulebook entry can be written against evidence.
            "unmatched_styles": numbering.get("styles_by_part", {}).get(part.slug, {}),
            "unmatched_examples": numbering.get("examples_by_part", {}).get(part.slug, [])[:6],
            "orphan_residual_ink": residual,
            "part_ink": part_ink,
            "orphan_rate": round(orphan_rate, 6),
            "geometry_disagreements": geometry_bad,
            "geometry_tested": geometry_tested,
            "geometry_rate": round(geometry_rate, 4),
            "checks_run": sorted(CHECK_IDS),
            "alarms": alarms,
            "passed": not alarms,
        }
    return out


def evaluate_fit(interpretation: dict, numbering: dict, probe: dict, thresholds: dict) -> dict:
    alarms: list[dict] = []

    if thresholds.get("require_interpretation_clause") and not interpretation["found"]:
        alarms.append(
            {
                "check": "interpretation_clause_missing",
                "detail": "no interpretation cue from the rulebook appears anywhere in the body",
                "examples": [],
            }
        )
    if interpretation["units_unknown_to_rulebook"]:
        alarms.append(
            {
                "check": "interpretation_names_unknown_units",
                "detail": "the document names units the rulebook does not declare: "
                + ", ".join(interpretation["units_unknown_to_rulebook"]),
                "examples": interpretation["examples"][:5],
            }
        )
    limit = thresholds["max_unmatched_numbering_rate"]
    if numbering["rate"] > limit:
        alarms.append(
            {
                "check": "unmatched_numbering",
                "detail": f"{numbering['unmatched_lines']} of {numbering['numbered_lines']} "
                f"numbered lines match no rulebook pattern, rate {numbering['rate']} > {limit}",
                "examples": numbering["examples"][:10],
            }
        )
    limit = thresholds["max_orphan_block_rate"]
    if probe["orphan"]["rate"] > limit:
        alarms.append(
            {
                "check": "orphan_text",
                "detail": f"{probe['orphan']['residual_ink']:+d} of "
                f"{probe['orphan']['body_ink']} body ink characters unaccounted for, "
                f"rate {probe['orphan']['rate']} > {limit}",
                "examples": probe["orphan"]["examples"][:10],
            }
        )
    depth = probe["depth"]
    if depth["max_dotted_depth"] > depth["rulebook_max_dotted_depth"]:
        alarms.append(
            {
                "check": "depth_out_of_range",
                "detail": f"numbering reaches {depth['max_dotted_depth']} dotted levels, "
                f"the rulebook allows {depth['rulebook_max_dotted_depth']}",
                "examples": depth["examples"][:10],
            }
        )
    limit = thresholds["max_geometry_disagreement"]
    geometry = probe["geometry"]
    if geometry["pairs_tested"] and geometry["rate"] > limit:
        alarms.append(
            {
                "check": "geometry_disagrees_with_numbering",
                "detail": f"{geometry['disagreements']} of {geometry['pairs_tested']} numbered "
                f"lines sit left of their level's parent, rate {geometry['rate']} > {limit}",
                "examples": geometry["examples"][:10],
            }
        )
    return {
        "interpretation_clause": interpretation,
        "numbering": numbering,
        "orphan_text": probe["orphan"],
        "depth": depth,
        "geometry": geometry,
        "alarms": alarms,
        "passed": not alarms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.profile")
    parser.add_argument("--pages", nargs=2, type=int, default=None, metavar=("FIRST", "LAST"))
    args = parser.parse_args(argv)

    page_range = tuple(args.pages) if args.pages else None
    document = scan(config.PDF, config.BATCHES, page_range=page_range)
    assigned, scores = assign_rulebook(document, config.HIERARCHY_PROFILES)
    rulebook = Rulebook(assigned, config.HIERARCHY_PROFILES[assigned])

    interpretation = find_interpretation(document, rulebook)
    numbering = numbering_coverage(document, rulebook, config.PDF)
    probe = probe_parts(config.PDF, document, rulebook)
    fit = evaluate_fit(interpretation, numbering, probe, config.QUARANTINE_THRESHOLDS)
    per_part = fit_by_part(
        numbering, probe, document, config.QUARANTINE_THRESHOLDS, interpretation
    )

    pages = profile_pages(document)
    report = {
        "document": {
            "id": config.DOCUMENT_ID,
            "source_file": config.PDF.name,
            "source_sha256": document.sha256,
            "page_count": document.page_count,
            "pages_profiled": [min(document.pages), max(document.pages)],
            "file_author": document.metadata.get("author") or None,
            "file_created": document.metadata.get("creationDate") or None,
            "producer": document.metadata.get("producer") or None,
            "tagged": document.tagged,
            # Existence only; the outline's contents belong to stage 8.
            "has_outline": document.has_outline,
            "pages_with_text_layer": sum(1 for p in pages if p["has_text_layer"]),
            "pages_without_text_layer": sum(1 for p in pages if not p["has_text_layer"]),
            "font_size_histogram": document.font_histogram(),
        },
        "profile": {
            "assigned": assigned,
            "candidates": scores,
            "levels": rulebook.levels,
            "max_dotted_depth": rulebook.max_dotted_depth,
        },
        "parts": [
            {
                "id": part.slug,
                "title": part.title,
                "family": part.family,
                "page_start": part.page_start,
                "page_end": part.page_end,
                "template_version_raw": part.model_version_raw or part.header_version_raw,
                "version_source": part.version_source,
                "anomalies": part.anomalies,
            }
            for part in document.parts
        ],
        "derived_part_count": len(document.parts),
        "fit": fit,
        "fit_by_part": per_part,
        "pages": pages,
    }

    config.OUTPUT.mkdir(parents=True, exist_ok=True)
    profile_path = config.OUTPUT / "profile.json"
    profile_path.write_text(dump_json(report), encoding="utf-8")
    quarantine_path = config.OUTPUT / "quarantine.json"

    print(f"profile: {document.page_count} pages, {len(document.parts)} parts derived")
    print(f"profile: rulebook {assigned!r} assigned, "
          f"grammar covers {scores[assigned]['coverage']:.1%} of numbered lines")
    print(f"profile: interpretation clause found={fit['interpretation_clause']['found']} "
          f"({fit['interpretation_clause']['cue_hits']} cue hits), "
          f"units named {sorted(fit['interpretation_clause']['units_named_by_document'])}")
    print(f"profile: unmatched numbering {numbering['unmatched_lines']}/{numbering['numbered_lines']} "
          f"= {numbering['rate']:.4f} (limit {config.QUARANTINE_THRESHOLDS['max_unmatched_numbering_rate']})")
    print(f"profile: orphan text {probe['orphan']['residual_ink']:+d}/"
          f"{probe['orphan']['body_ink']} ink "
          f"= {probe['orphan']['rate']:.4f} (limit "
          f"{config.QUARANTINE_THRESHOLDS['max_orphan_block_rate']}, signed residual)")
    print(f"profile: max dotted depth {probe['depth']['max_dotted_depth']} "
          f"(rulebook allows {probe['depth']['rulebook_max_dotted_depth']})")
    print(f"profile: geometry {probe['geometry']['disagreements']}/{probe['geometry']['pairs_tested']} "
          f"= {probe['geometry']['rate']:.4f} (limit {config.QUARANTINE_THRESHOLDS['max_geometry_disagreement']}), "
          f"{len(probe['geometry']['parts_measured'])} parts measured, "
          f"{len(probe['geometry']['parts_abstained'])} abstained")
    print(f"profile: written to {profile_path.relative_to(config.ROOT)}")

    failed_parts = sorted(p for p, v in per_part.items() if not v["passed"])
    passed_parts = sorted(p for p, v in per_part.items() if v["passed"])
    print(f"profile: per-part fit, {len(passed_parts)} parts pass, {len(failed_parts)} quarantined"
          + (f": {', '.join(failed_parts)}" if failed_parts else ""))

    if fit["alarms"]:
        quarantine_path.write_text(
            dump_json(
                {
                    "document": config.DOCUMENT_ID,
                    "source_sha256": document.sha256,
                    "profile_considered": assigned,
                    "alarms": fit["alarms"],
                    "parts_quarantined": {p: per_part[p] for p in failed_parts},
                    "parts_passing": passed_parts,
                    "action": "quarantined, nothing loaded; a person decides which rulebook this needs",
                }
            ),
            encoding="utf-8",
        )
        for alarm in fit["alarms"]:
            print(f"profile: ALARM {alarm['check']}: {alarm['detail']}", file=sys.stderr)
        print(f"profile: quarantined, {quarantine_path.relative_to(config.ROOT)} written, "
              f"nothing loaded", file=sys.stderr)
        return 2

    if quarantine_path.exists():
        quarantine_path.unlink()
    print("profile: all five fit checks pass, not quarantined")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
