"""`outline_vs_provided`: the derived tree against the PDF's embedded outline.

SPEC 2.6: "per part, agree count, parser_wrong, outline_wrong, both_differ,
from a sampled triage."

The outline is machine generated from the source document's styling and, as the
assignment notes say itself, "not uniformly reliable below the top level". So
this section never rules for either side. It automatically counts **agreement**,
where both descriptions say the same thing, and everything else goes into a
triage queue for a human, counted **unreviewed** until a verdict for that queue
id appears in `golden/decisions.jsonl` (kind `anomaly`, verdicts
`agree` / `parser_wrong` / `outline_wrong` / `both_differ`).

An unreviewed disagreement is not evidence against the parser and is not
evidence for it. It is printed as work outstanding.
"""
from __future__ import annotations

from typing import Any, Optional

from pipeline.eval.context import (Context, LIST_CAP, OUTLINE_TITLE_AGREE,
                                   OUTLINE_TRIAGE_SAMPLE)
from pipeline.eval.golden import ANOMALY_KIND, TRIAGE_VERDICTS
from pipeline.eval.inputs import walk
from pipeline.eval.provided import OutlineEntry
from pipeline.eval.rates import MEASURED, NO_DATA, Rate, Section, cap
from pipeline.eval.sampling import stratified_sample
from pipeline.eval.text import normalise, similarity
from pipeline.schemas import Node

COMPARABLE_KINDS = ("part", "heading", "preamble", "clause", "subclause", "item",
                    "form_row", "table")


def comparable_nodes(tree: Node) -> list[Node]:
    """Derived nodes the outline could plausibly have an entry for."""
    return [n for n in walk(tree)
            if n.kind in COMPARABLE_KINDS and (n.label or n.title)]


def _label_key(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    return normalise(label).strip("()")


def _window(ctx: Context, part: str) -> tuple[Optional[tuple[int, int]], str]:
    """Absolute page window for a part's outline entries.

    The provided page map is used where it names the part, because a derived
    tree's own page numbers may be fixture-local or wrong, and using them to
    pick the comparison set would let a page error hide a structure error.
    """
    for row in ctx.page_map.rows:
        if row.part_id == part:
            return row.pages, f"provided page map row {row.row_index}"
    tree = ctx.inputs.trees.get(part)
    if tree is not None:
        return (tree.page_start, tree.page_end), "derived part page range"
    return None, "no page range available"


def triage(ctx: Context, part: str) -> dict[str, Any]:
    tree = ctx.inputs.trees[part]
    window, window_source = _window(ctx, part)
    if window is None:
        return {"part": part, "status": "no_data", "reason": window_source}
    entries = ctx.outline.in_pages(*window)
    nodes = comparable_nodes(tree)

    by_label: dict[str, list[Node]] = {}
    for n in nodes:
        key = _label_key(n.label)
        if key:
            by_label.setdefault(key, []).append(n)

    matched_nodes: set[str] = set()
    agreements: list[dict[str, Any]] = []
    disagreements: list[dict[str, Any]] = []

    for e in entries:
        node = None
        how = None
        key = _label_key(e.label)
        if key and len(by_label.get(key, [])) == 1:
            node, how = by_label[key][0], "label"
        elif e.stripped_title:
            best, best_score = None, 0.0
            for n in nodes:
                if n.path in matched_nodes or not n.title:
                    continue
                score = similarity(e.stripped_title, n.title)
                if score > best_score:
                    best, best_score = n, score
            if best is not None and best_score >= OUTLINE_TITLE_AGREE:
                node, how = best, "title"
        if node is None:
            disagreements.append({
                "queue_id": f"outline:{part}#{e.index}",
                "difference": "outline_entry_with_no_derived_node",
                "outline": e.as_dict(), "derived": None,
            })
            continue

        matched_nodes.add(node.path)
        title_score = (similarity(e.stripped_title, node.title)
                       if (e.stripped_title and node.title) else None)
        label_agrees = (key is None or _label_key(node.label) is None
                        or key == _label_key(node.label))
        title_agrees = (title_score is None or title_score >= OUTLINE_TITLE_AGREE)
        page_agrees = node.page_start <= e.page <= node.page_end
        record = {
            "queue_id": node.path,
            "outline": e.as_dict(),
            "derived": {"path": node.path, "label": node.label, "title": node.title,
                        "pages": [node.page_start, node.page_end]},
            "matched_by": how,
            "title_similarity": title_score,
            "page_agrees": page_agrees,
        }
        if label_agrees and title_agrees:
            record["agreement"] = ("labels and titles agree" if title_score is not None
                                   else "labels agree, no title on one side")
            agreements.append(record)
        else:
            record["difference"] = ("title_differs" if not title_agrees
                                    else "label_differs")
            disagreements.append(record)

    for n in nodes:
        if n.path in matched_nodes:
            continue
        disagreements.append({
            "queue_id": n.path,
            "difference": "derived_node_with_no_outline_entry",
            "outline": None,
            "derived": {"path": n.path, "label": n.label, "title": n.title,
                        "pages": [n.page_start, n.page_end]},
        })

    return {"part": part, "status": "measured", "window": list(window),
            "window_source": window_source,
            "outline_entries_in_window": len(entries),
            "derived_comparable_nodes": len(nodes),
            "agreements": agreements, "disagreements": disagreements}


def whole_document(ctx: Context, per_part: list[dict[str, Any]]) -> dict[str, Any]:
    """What `--full` claims: the whole outline accounted for, not just the parts
    this run has trees for. Everything outside an in-scope window is outline the
    pipeline has not been compared against at all, which is a different fact
    from a disagreement and is counted as its own number."""
    covered: set[int] = set()
    for p in per_part:
        if p.get("status") != "measured":
            continue
        first, last = p["window"]
        covered.update(e.index for e in ctx.outline.in_pages(first, last))
    uncompared = [e for e in ctx.outline.entries if e.index not in covered]
    by_part: dict[str, int] = {}
    for e in uncompared:
        row = next((r for r in ctx.page_map.rows
                    if r.pages[0] <= e.page <= r.pages[1]), None)
        by_part[row.part_id if row else "outside every provided row"] = \
            by_part.get(row.part_id if row else "outside every provided row", 0) + 1
    return {
        "outline_entries_total": len(ctx.outline.entries),
        "compared_against_a_derived_tree": Rate(len(covered),
                                                len(ctx.outline.entries)).as_dict(),
        "not_compared_because_the_part_has_no_derived_tree": len(uncompared),
        "not_compared_by_part": dict(sorted(by_part.items())),
    }


def build(ctx: Context) -> Section:
    s = Section("outline_vs_provided")
    s.data["outline"] = {"state": ctx.outline.state, "source_file": ctx.outline.source_file,
                         "entries": len(ctx.outline.entries), "error": ctx.outline.error,
                         "pdf_page_count": ctx.outline.page_count,
                         "top_level_entries": len(ctx.outline.level1())
                         if ctx.outline.state == "loaded" else None}
    if ctx.outline.state != "loaded":
        s.status = NO_DATA
        s.reason = ctx.outline.error or "embedded outline unavailable"
        s.line(f"_{s.reason}_")
        return s
    if not ctx.inputs.trees:
        s.status = NO_DATA
        s.reason = "no stage 2 trees loaded; nothing to diff the outline against"
        s.line(f"_{s.reason}_ The outline itself loaded: "
               f"{len(ctx.outline.entries)} entries, "
               f"{len(ctx.outline.level1())} at the top level.")
        return s

    # Verdicts a human has already given, keyed by the queue id in `path`.
    verdicts = {r.path: r for r in ctx.golden.of_kind(ANOMALY_KIND)
                if r.verdict in TRIAGE_VERDICTS and r.path}

    per_part: list[dict[str, Any]] = []
    all_disagreements: list[dict[str, Any]] = []
    total_agree = total_items = 0
    for part in sorted(ctx.inputs.trees):
        result = triage(ctx, part)
        if result["status"] != "measured":
            per_part.append(result)
            continue
        for d in result["disagreements"]:
            rec = verdicts.get(d["queue_id"])
            d["part"] = part
            d["verdict"] = rec.verdict if rec else "unreviewed"
            d["reviewer"] = rec.reviewer if rec else None
            all_disagreements.append(d)
        counts = {v: sum(1 for d in result["disagreements"]
                         if (verdicts[d["queue_id"]].verdict if d["queue_id"] in verdicts
                             else "unreviewed") == v)
                  for v in (*TRIAGE_VERDICTS, "unreviewed")}
        agree_auto = len(result["agreements"])
        items = agree_auto + len(result["disagreements"])
        total_agree += agree_auto + counts["agree"]
        total_items += items
        per_part.append({
            "part": part, "status": "measured",
            "window": result["window"], "window_source": result["window_source"],
            "outline_entries_in_window": result["outline_entries_in_window"],
            "derived_comparable_nodes": result["derived_comparable_nodes"],
            "agree_automatic": agree_auto,
            "disagreements": len(result["disagreements"]),
            "triage": counts,
            "page_disagreements": sum(1 for a in result["agreements"]
                                      if not a["page_agrees"]),
        })

    unreviewed = [d for d in all_disagreements if d["verdict"] == "unreviewed"]
    sample = stratified_sample(
        unreviewed,
        lambda d: (d["part"], d["difference"]),
        OUTLINE_TRIAGE_SAMPLE if not ctx.full else max(OUTLINE_TRIAGE_SAMPLE, len(unreviewed)),
        ["part", "difference"],
        seed_material=f"outline-triage|{ctx.run}",
    )
    queue = [unreviewed[i] for i in sample.indices]

    s.status = MEASURED
    s.data["per_part"] = per_part
    s.data["totals"] = {
        "compared_items": total_items,
        "agree": Rate(total_agree, total_items).as_dict(),
        "disagreements": len(all_disagreements),
        "triage": {v: sum(1 for d in all_disagreements if d["verdict"] == v)
                   for v in (*TRIAGE_VERDICTS, "unreviewed")},
    }
    s.data["triage_sample"] = sample.as_dict()
    s.data["triage_queue"] = cap(queue, LIST_CAP)[0]
    s.data["triage_queue_not_listed"] = cap(queue, LIST_CAP)[1]
    if ctx.full:
        s.data["whole_document"] = whole_document(ctx, per_part)

    s.line(f"Embedded outline: **{len(ctx.outline.entries)}** entries "
           f"({len(ctx.outline.level1())} top level) from `{ctx.outline.source_file}`.")
    if ctx.inputs.source == "fixtures":
        s.line("_Fixture trees are two-clause excerpts with synthetic titles, so most "
               "outline entries in a part's window have no derived node. That is the "
               "harness working, not a parser result._")
    s.line()
    s.line(f"Automatic agreement across parts: **{Rate(total_agree, total_items)}**. "
           f"No disagreement is scored against either side without a human verdict.")
    s.line()
    s.table(["part", "window", "window from", "outline entries", "derived nodes",
             "agree", "disagree", "parser_wrong", "outline_wrong", "both_differ",
             "unreviewed"],
            [[p["part"], f'{p["window"][0]}-{p["window"][1]}', p["window_source"],
              p["outline_entries_in_window"], p["derived_comparable_nodes"],
              p["agree_automatic"], p["disagreements"],
              p["triage"]["parser_wrong"], p["triage"]["outline_wrong"],
              p["triage"]["both_differ"], p["triage"]["unreviewed"]]
             for p in per_part if p["status"] == "measured"])
    s.line()
    s.line(f"**Triage queue**, {len(unreviewed)} unreviewed disagreement(s), "
           f"{sample.size} drawn for this run "
           f"(stratified by {', '.join(sample.strata_names)}):")
    s.table(["queue id", "difference", "outline says", "derived says", "title sim"],
            [[d["queue_id"], d["difference"],
              (d["outline"] or {}).get("title", "—"),
              ((d["derived"] or {}).get("title")
               or (d["derived"] or {}).get("label") or "—"),
              d.get("title_similarity")] for d in cap(queue, LIST_CAP)[0]])
    if cap(queue, LIST_CAP)[1]:
        s.line(f"_{cap(queue, LIST_CAP)[1]} further queued item(s) in report.json._")
    s.bullet("a verdict is recorded by appending "
             '`{"kind": "anomaly", "path": "<queue id>", "verdict": '
             '"parser_wrong|outline_wrong|both_differ|agree", ...}` to golden/decisions.jsonl')
    if ctx.full:
        wd = s.data["whole_document"]
        r = wd["compared_against_a_derived_tree"]
        s.line()
        s.line(f"**Whole document** (`--full`): "
               f"**{Rate(r['count'], r['of'])}** outline entries were compared against a "
               f"derived tree. The rest belong to parts this run has no tree for, which "
               f"is not a disagreement.")
        s.table(["part the entry falls in", "entries not compared"],
                [[k, v] for k, v in wd["not_compared_by_part"].items()][:LIST_CAP])
    return s
