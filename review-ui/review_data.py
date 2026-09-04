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


# --------------------------------------------------------------------------
# Plain English. The reader is a contracts or procurement specialist, not an
# engineer: they know what a schedule and a defined term are, and they have
# never seen a node path, a resolver or a confidence score. Every string a
# reviewer reads is composed here rather than in the page, so it is one place
# to change and `tests/review_ui/test_copy.py` can hold it to its promises.
# --------------------------------------------------------------------------

FAMILY_WORDS = {
    "core-terms": ("the Core Terms", "the clauses that govern the agreement as a whole"),
    "award-form": ("the Framework Award Form", "the form that records who the agreement is with"),
    "framework-schedule": ("a Framework Schedule", "part of the framework agreement itself"),
    "joint-schedule": ("a Joint Schedule", "shared by the framework agreement and the contracts called off under it"),
    "call-off-schedule": ("a Call-Off Schedule", "part of an individual contract called off under the framework"),
}

# What each row type asks of a reviewer, shown above its section.
GUIDELINES = {
    "ref": {
        "title": "Cross-references",
        "summary": "Places where one part of the agreement points at another part, "
                   "and the system could not be certain which part is meant.",
        "how": [
            "Read the sentence. The words that do the pointing are highlighted.",
            "Check the page image beside it: that is the actual page, with the "
            "pointing words boxed.",
            "Choose the part the writer meant. If the target is not in this "
            "document set at all, say so — that is a useful answer, not a failure.",
            "If the highlighted words are not a cross-reference at all, say that instead.",
        ],
        "why": "Your answer becomes the standard the system is measured against, so "
               "an honest “I cannot tell from this” is worth more than a guess.",
    },
    "term": {
        "title": "Defined terms",
        "summary": "The agreement gives certain capitalised words a special meaning. "
                   "These are places where the system is unsure whether the capitals "
                   "signal that meaning or are just ordinary writing.",
        "how": [
            "Read the sentence and decide whether the writer meant the defined term.",
            "The definition that would apply here is shown, so you can check it fits.",
            "If the capitals are incidental — the start of a sentence, or a heading — "
            "then it is ordinary words, not the defined term.",
            "If it is a use but of a different defined term, name that one instead.",
        ],
        "why": "Defined terms carry obligations. A word wrongly treated as defined "
               "changes what the clause appears to require.",
    },
    "anomaly": {
        "title": "Flagged oddities",
        "summary": "Things on the page that look wrong or unusual. The wording is always "
                   "kept exactly as printed; nothing is corrected.",
        "how": [
            "Compare the text with the page image beside it.",
            "Where a reading is proposed, decide whether it is what the document means.",
            "Where none is proposed, decide whether the flag is fair.",
            "Either way the original text stays as it is. You are labelling it, not editing it.",
        ],
        "why": "A typo in a contract is part of the contract. Recording it as an "
               "oddity, rather than silently fixing it, is what keeps the record honest.",
    },
}


def family_words(path: str) -> tuple[str, str]:
    """(what it is, what that means) for the part a path belongs to."""
    part = part_of(path)
    for prefix, words in FAMILY_WORDS.items():
        if part == prefix or part.startswith(prefix + "-"):
            return words
    return (part.replace("-", " "), "")


def title_case_part(part: str) -> str:
    """`framework-schedule-2` -> `Framework Schedule 2`."""
    words = []
    for token in part.split("-"):
        if token.isdigit():
            words.append(token)
        elif token.lower() == "off":
            words[-1] = words[-1] + "-Off"
        else:
            words.append(token.capitalize())
    return " ".join(words)


def human_citation(c, node) -> str:
    """"Core Terms, Clause 9.2" rather than "core-terms/9/9.2".

    Built from the part's own title and the deepest numbered unit, using the
    unit label the document itself uses (SPEC 2.1), so the reviewer sees the
    reference the way the agreement writes it.
    """
    if node is None:
        return ""
    part_node = c.node(part_of(node.path))
    part_name = (part_node.title if part_node is not None and part_node.title
                 else title_case_part(part_of(node.path)))
    if node.kind == "part":
        return part_name

    # Cells and intros carry no number of their own, so name them by what they
    # are rather than by the nearest numbered ancestor, which would read as if
    # the whole clause were meant.
    owner = next((n for n in reversed(c.ancestors(node.path)) if n.label), None)
    if node.kind == "cell":
        label_cell = _definition_label_cell(c, node)
        if label_cell is not None and label_cell.text:
            return f"{part_name}, the definition of {label_cell.text.strip()}"
        if node.col == 0 and node.text and node.text.strip().startswith('"'):
            return f"{part_name}, the term {node.text.strip()}"
        if owner is not None:
            role = {"label": "the label", "value": "the entry",
                    "header": "the heading"}.get(node.cell_role or "", "a cell")
            noun = "row" if owner.kind == "form_row" else (owner.unit_label or "row")
            return f"{part_name}, {noun} {owner.label}, {role}"
        return part_name
    if node.kind == "intro" and owner is not None:
        return f"{part_name}, {(owner.unit_label or 'Clause')} {owner.label}, opening words"

    chain = [n for n in (c.ancestors(node.path) + [node]) if n.label and n.kind != "part"]
    if not chain:
        return part_name

    deepest = chain[-1]
    unit = deepest.unit_label or {
        "form_row": "row", "table": "table", "item": "paragraph",
    }.get(deepest.kind, deepest.kind.capitalize())
    text = f"{part_name}, {unit} {deepest.label}"
    if len(chain) > 1 and deepest.label.startswith("("):
        parent = chain[-2]
        punit = parent.unit_label or "Clause"
        text = f"{part_name}, {punit} {parent.label}, {unit.lower()} {deepest.label}"
    return text


def _definition_label_cell(c, node):
    """For a definitions-table value cell, the cell holding the quoted term."""
    parts = node.path.rsplit("/", 2)
    if len(parts) != 3 or node.col in (None, 0):
        return None
    return c.node(f"{parts[0]}/{parts[1]}/0")


def describe_candidate(c, cand) -> dict:
    """A candidate target in words a contracts reviewer already uses."""
    target = c.node(cand.path)
    if target is not None:
        name = human_citation(c, target)
        _, meaning = family_words(cand.path)
        loaded = True
    else:
        name = title_case_part(cand.path)
        _, meaning = family_words(cand.path)
        loaded = False
    return {
        "path": cand.path,
        "name": name,
        "meaning": meaning,
        "loaded": loaded,
        "score": cand.score,
        "reason": cand.reason,
    }


def _confidence_words(cands: list[dict]) -> str | None:
    """Say what the numbers mean, or say nothing. A bare 0.5 tells a reviewer
    neither what it measures nor which direction is good."""
    scores = [c["score"] for c in cands if c["score"] is not None]
    if len(scores) < 2:
        return None
    if max(scores) - min(scores) < 0.01:
        return "The system found nothing to prefer one over the other."
    best = max(cands, key=lambda c: c["score"] if c["score"] is not None else -1)
    return f"The system leaned towards {best['name']}, but not enough to be sure."


def ref_copy(c, ref, cands: list[dict]) -> dict:
    """What this cross-reference row asks, and the answers available."""
    quoted = f'“{ref.text}”'
    situation = f"This sentence points at {quoted}."

    named = [cd for cd in cands if cd["loaded"]]
    unloaded = [cd for cd in cands if not cd["loaded"]]

    if len(cands) > 1:
        explain = (f"This document set contains more than one thing called {quoted}."
                   if ref.ref_kind in ("schedule", "annex", "part")
                   else f"More than one part of the agreement could be meant by {quoted}.")
        question = "Which one does the writer mean?"
    elif unloaded:
        explain = (f"It looks like {unloaded[0]['name']}, but that part has not been "
                   "loaded into the system yet, so the pointer could not be followed.")
        question = f"Is {unloaded[0]['name']} what the writer meant?"
    elif named:
        explain = "The system found one possible target but could not confirm it."
        question = f"Is {named[0]['name']} what the writer meant?"
    else:
        explain = "Nothing in the loaded document set matches it."
        question = "Does this point at something outside this document set?"

    answers = [
        {"verdict": "target", "chosen_candidate": cd["path"],
         "label": cd["name"], "sublabel": cd["meaning"], "kind": "candidate",
         "loaded": cd["loaded"]}
        for cd in cands
    ]
    answers.append({"verdict": "unresolvable", "kind": "other",
                    "label": "None of these",
                    "sublabel": "it points at something this document set does not contain"})
    answers.append({"verdict": "not_a_reference", "kind": "other",
                    "label": "It is not a cross-reference",
                    "sublabel": f"{quoted} is ordinary wording here"})
    return {"situation": situation, "explain": explain, "question": question,
            "answers": answers, "confidence_words": _confidence_words(cands)}


AMBIGUITY_WORDS = {
    "sentence_initial": "Here it sits at the start of a sentence, so the capital letter "
                        "may just be normal punctuation rather than the defined term.",
    "heading": "Here it appears in a heading, where words are capitalised anyway.",
    "typo_dense": "This section has enough spelling irregularities that its capitalisation "
                  "is not a reliable signal.",
    "alias_collision": "This short form could stand for more than one defined term.",
    "none": "The system flagged this use for a second opinion.",
}


def term_copy(c, use, node, site_node, options: list[str]) -> dict:
    quoted = f"“{use.term}”"
    situation = f"{quoted} is a term this agreement defines."
    explain = AMBIGUITY_WORDS.get(use.ambiguity_kind, AMBIGUITY_WORDS["none"])
    question = "Is this the defined term, or ordinary words?"
    answers = [
        {"verdict": "use", "chosen_candidate": use.term, "kind": "primary",
         "label": f"Yes, it means {quoted}",
         "sublabel": "the defined term applies here"},
        {"verdict": "not_a_use", "kind": "other",
         "label": "No, these are ordinary words",
         "sublabel": "the capitals do not signal the defined term"},
    ]
    return {"situation": situation, "explain": explain, "question": question,
            "answers": answers,
            "other_terms_hint": ("If it is a use but of a different defined term, "
                                 "name that one first." if len(options) > 1 else None)}


ANOMALY_WORDS = {
    "stray_character_in_label": "That looks like a stray character.",
    "numbering_gap": "The numbering skips a number.",
}


def anomaly_copy(c, node, parsed: dict, anchor: str) -> dict:
    code = parsed["code"] or "anomaly"
    base = next((v for k, v in ANOMALY_WORDS.items() if code.startswith(k)), None)

    if parsed["proposed"]:
        situation = f"The text here reads “{anchor}”."
        explain = ((base or "The system flagged this as unusual.")
                   + " The wording is kept exactly as printed; nothing has been changed.")
        question = f"Should this be read as “{parsed['proposed']}”?"
        answers = [
            {"verdict": "confirmed", "kind": "primary",
             "label": f"Yes, it means “{parsed['proposed']}”",
             "sublabel": "record the reading beside the original wording"},
            {"verdict": "rejected", "kind": "other",
             "label": "No, that reading is wrong",
             "sublabel": "leave it flagged with no accepted reading"},
        ]
    else:
        detail = parsed["detail"] or code.replace("_", " ")
        situation = f"The system flagged something unusual here: {detail}."
        explain = ((base + " ") if base else "") + (
            "This may be how the document is drafted, or a sign the page was misread.")
        question = "Is this fairly flagged?"
        answers = [
            {"verdict": "confirmed", "kind": "primary",
             "label": "Yes, that is worth flagging",
             "sublabel": "keep it on the record"},
            {"verdict": "rejected", "kind": "other",
             "label": "No, nothing is wrong here",
             "sublabel": "dismiss the flag"},
        ]
    return {"situation": situation, "explain": explain, "question": question,
            "answers": answers}


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
        cands = [describe_candidate(c, cd) for cd in ref.candidates]
        if ref.target_path and not any(cd["path"] == ref.target_path for cd in cands):
            target = c.node(ref.target_path)
            cands.insert(0, {
                "path": ref.target_path,
                "name": human_citation(c, target) if target else title_case_part(ref.target_path),
                "meaning": family_words(ref.target_path)[1],
                "loaded": target is not None,
                "score": None,
                "reason": "the system's own answer",
            })
        rows.append(
            {
                "id": ref.path,
                "kind": "ref",
                "part": part_of(ref.path),
                "path": ref.path,
                "node_id": parent.id if parent is not None else None,
                "parent_path": parent_path,
                "status": ref.status,
                "citation": human_citation(c, parent),
                "copy": ref_copy(c, ref, cands),
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
                "candidate_cards": cands,
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
                "citation": human_citation(c, node),
                "copy": term_copy(c, use, node, site_node, options),
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
                    "citation": human_citation(c, node),
                    "copy": anomaly_copy(c, node, parsed, anchor),
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
