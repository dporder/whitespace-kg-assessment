"""Builds the review queue from stage output.

Three things land in the queue, per SPEC 6 and DESIGN.md section 8:

  ref      every ref whose status is ambiguous or unresolved
  term     every term use whose status is ambiguous
  anomaly  every node carrying an anomaly, with a proposed reading where the
           recorded string offers one

Each row carries the source sentence with the span that triggered it, a crop
URL resolving to a server-rendered page image, and the candidates where there
are any.

This module is the single import site for the shared substrate. chat/source.py
and chat/crops.py serve both UIs because `review-ui` contains a hyphen and so
is not an importable package name, and the SPEC section 1 ownership map gives
ui-builder no third directory to put shared code in. Move those two modules and
only this file changes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import config as ui_config          # noqa: E402  the one DATA_SOURCE switch
from chat import crops                        # noqa: E402
from chat.source import corpus, parent_path_of_ref, part_of   # noqa: E402

REVIEWABLE_REF_STATUS = ("ambiguous", "unresolved")

# Anomaly strings follow the convention `<code>: <detail>` used throughout the
# fixtures and SPEC 2.1 examples. Where the detail names a reading, as in
# "stray_character_in_label: 'rFramework' for 'Framework', recorded verbatim",
# it is surfaced as a proposal. schemas.py has no dedicated field for a model
# proposed reading and its confidence, so this parses the convention rather
# than reading a typed field; see review-ui/README.md.
_PROPOSED = re.compile(r"'(?P<found>[^']+)'\s+for\s+'(?P<proposed>[^']+)'")


def crop_url(page: int, bbox, colour: str) -> str:
    return "/api/crop?" + urlencode(
        {"page": page, "bbox": ",".join(f"{v:g}" for v in bbox), "colour": colour}
    )


def _box_of(node) -> dict | None:
    boxes = node.bboxes_own or node.bboxes_extent
    if not boxes:
        return None
    b = boxes[0]
    return {"page": b.page, "bbox": list(b.bbox)}


def _crop(node, colour: str) -> dict | None:
    box = _box_of(node)
    if box is None:
        return None
    return {
        "page": box["page"],
        "bbox": box["bbox"],
        "colour": colour,
        "url": crop_url(box["page"], box["bbox"], colour),
    }


def _unit(node, c=None) -> str:
    """"Clause 9.2", or "Clause 9.1 lead-in" for the intro child of a container.

    An intro node carries no label of its own (SPEC 2.1), so name it by the
    container whose lead-in words it holds.
    """
    if node.kind == "intro" and c is not None:
        parent = c.node(node.path.rsplit("/", 1)[0])
        if parent is not None:
            return f"{_unit(parent, c)} lead-in"
    if node.kind == "cell" and c is not None:
        parent = c.node(node.path.rsplit("/", 1)[0])
        if parent is not None and parent.kind in ("form_row", "table"):
            return f"{_unit(parent, c)} {node.cell_role or 'cell'}"
    bits = [node.unit_label or node.kind.replace("_", " ").capitalize()]
    if node.label:
        bits.append(node.label)
    return " ".join(bits)


def parse_anomaly(text: str, anchor: str | None = None) -> dict:
    """Split `<code>: <detail>` and, where the detail names a correction,
    derive the reading a reviewer would be accepting.

    `found_token` and `proposed_token` are quoted verbatim from the anomaly.
    `proposed` is the node's own text with that one substitution applied, which
    is what the reviewer actually judges. It is an interpretation, shown beside
    the raw text and never replacing it; the substitution is only offered when
    the token really occurs in the text, so nothing is invented.
    """
    code, _, detail = text.partition(":")
    out = {
        "raw": text,
        "code": code.strip(),
        "detail": detail.strip() or None,
        "found_token": None,
        "proposed_token": None,
        "proposed": None,
    }
    m = _PROPOSED.search(text)
    if m:
        found, proposed = m.group("found"), m.group("proposed")
        out["found_token"], out["proposed_token"] = found, proposed
        if anchor and found in anchor:
            out["proposed"] = anchor.replace(found, proposed, 1)
        elif not anchor:
            out["proposed"] = proposed
    return out


# --------------------------------------------------------------------------
def ref_rows(c) -> list[dict]:
    rows = []
    for ref in c.refs:
        if ref.status not in REVIEWABLE_REF_STATUS:
            continue
        parent_path = parent_path_of_ref(ref.path)
        parent = c.node(parent_path)
        sentence = c.anchor_text(parent) if parent is not None else ""
        rows.append(
            {
                "id": ref.path,
                "kind": "ref",
                "part": part_of(ref.path),
                "path": ref.path,
                "node_id": parent.id if parent is not None else None,
                "parent_path": parent_path,
                "status": ref.status,
                "label": (_unit(parent, c) if parent is not None else parent_path),
                "page": ref.page_start,
                "sentence": {
                    "text": sentence,
                    "span": list(ref.char_span) if ref.char_span else None,
                    "source": "text",
                },
                "crop": _crop(ref, "deterministic"),
                "candidates": [
                    {"path": cd.path, "score": cd.score, "reason": cd.reason}
                    for cd in ref.candidates
                ],
                "detail": {
                    "pointing_words": ref.text,
                    "ref_kind": ref.ref_kind,
                    "scope_rule": ref.scope_rule,
                    "resolver": ref.resolver,
                    "confidence": ref.confidence,
                    "target_path": ref.target_path,
                    "group_id": ref.group_id,
                },
            }
        )
    return rows


def term_options(c) -> list[str]:
    """Every defined term and alias, so a reviewer resolving an alias collision
    can name a governing term other than the one the pipeline matched. That name
    goes in `chosen_candidate`, which is what the harness reads."""
    names: set[str] = set()
    for site in c.definition_sites:
        names.add(site.term)
        names.update(site.aliases)
    return sorted(names)


def term_rows(c) -> list[dict]:
    rows = []
    options = term_options(c)
    for use in c.term_uses:
        if use.status != "ambiguous":
            continue
        node = c.by_id.get(use.node_id)
        if node is None:
            continue
        anchor = c.anchor_text(node)
        site = c.governing_site(use.term, part_of(node.path))
        site_node = c.by_id.get(site.definition_node_id) if site else None
        rows.append(
            {
                "id": f"{use.node_id}:{use.char_span[0]}-{use.char_span[1]}",
                "kind": "term",
                "part": part_of(node.path),
                "path": node.path,
                "node_id": use.node_id,
                "char_span": list(use.char_span),
                "status": use.ambiguity_kind,
                "label": _unit(node, c),
                "page": node.page_start,
                "sentence": {
                    "text": anchor,
                    "span": list(use.char_span),
                    "source": "text" if node.text is not None else "title",
                },
                "crop": _crop(node, "rule"),
                "candidates": [],
                "detail": {
                    "term": use.term,
                    "term_options": options,
                    "ambiguity_kind": use.ambiguity_kind,
                    "method": use.method,
                    "definition_used": use.definition_used,
                    "governing_scope": site.scope if site else None,
                    "governing_path": site_node.path if site_node else None,
                    "governing_text": site_node.text if site_node else None,
                },
            }
        )
    return rows


def anomaly_rows(c) -> list[dict]:
    rows = []
    for path, node in c.by_path.items():
        if not node.anomalies:
            continue
        anchor = c.anchor_text(node)
        for i, raw in enumerate(node.anomalies):
            parsed = parse_anomaly(raw, anchor)
            rows.append(
                {
                    "id": f"{node.id}#{i}",
                    "kind": "anomaly",
                    "part": part_of(path),
                    "path": path,
                    "node_id": node.id,
                    "anomaly": raw,
                    "anomaly_index": i,
                    "status": parsed["code"] or "anomaly",
                    "label": _unit(node, c),
                    "page": node.page_start,
                    "sentence": {"text": anchor, "span": None,
                                 "source": "text" if node.text is not None else "title"},
                    "crop": _crop(node, "ink"),
                    "candidates": [],
                    "detail": parsed,
                }
            )
    return rows


_ORDER = {"ref": 0, "term": 1, "anomaly": 2}


def queue(kinds: tuple[str, ...] = ("ref", "term", "anomaly"),
          part: str | None = None,
          include_decided: bool = True) -> list[dict]:
    """The whole queue, sorted so refs lead and reading order holds within a part."""
    from review_decisions import decisions_by_target      # local: avoids a cycle

    c = corpus()
    rows: list[dict] = []
    if "ref" in kinds:
        rows += ref_rows(c)
    if "term" in kinds:
        rows += term_rows(c)
    if "anomaly" in kinds:
        rows += anomaly_rows(c)
    if part:
        rows = [r for r in rows if r["part"] == part]

    decided = decisions_by_target()
    for r in rows:
        r["decided"] = decided.get(r["id"])
    if not include_decided:
        rows = [r for r in rows if r["decided"] is None]

    rows.sort(key=lambda r: (_ORDER[r["kind"]], r["part"], r["page"], r["id"]))
    return rows


def counts(rows: list[dict]) -> dict:
    out = {"total": len(rows), "ref": 0, "term": 0, "anomaly": 0, "decided": 0, "parts": {}}
    for r in rows:
        out[r["kind"]] += 1
        if r.get("decided"):
            out["decided"] += 1
        out["parts"][r["part"]] = out["parts"].get(r["part"], 0) + 1
    return out


def source_info() -> dict:
    c = corpus()
    return {
        "data_source": ui_config.DATA_SOURCE,
        "data_root": str(c.root),
        "pdf": str(crops.PDF),
        "pdf_present": crops.PDF.exists(),
        "parts": sorted(c.trees),
        "nodes": len(c.by_path),
        "refs": len(c.refs),
        "term_uses": len(c.term_uses),
    }
