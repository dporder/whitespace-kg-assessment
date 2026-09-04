"""`stratified_audit`: sample drawn, strata, agreement rate, disagreements listed.

EVALUATION.md layer 4. Routing only self-declared hard cases for checking would
let a systematic error in the easy cases run silently, so a random sample of the
**confident** decisions goes to an independent checker on every load, stratified
by `config.AUDIT["strata"]` so the sample mirrors the population rather than
whatever was convenient.

The sampler is built and runs tonight; the checker is an LLM call and
`pipeline/llm.py` belongs to another worker and does not exist yet. So the
sample is drawn, printed and stored, the call sits behind a try-import, and the
section reports **"audit runner pending llm.py"** rather than an agreement rate
it did not measure. The gate reads `skipped_no_data` in that state.

The contract this section expects of `pipeline/llm.py`, when it lands:

    complete(task: str, prompt: str) -> str

where `task` selects the model from `config.MODELS` (this section passes
`"eval_judge"`) and the return value is the model's raw text. This section
parses a strict JSON array out of it and fails the item, not the run, if it
cannot. `--no-llm` skips the call entirely.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import config
from pipeline.eval.context import Context, LIST_CAP
from pipeline.eval.rates import MEASURED, NO_DATA, PARTIAL, Rate, Section
from pipeline.eval.sampling import position_bucket, stratified_sample, word_count_bucket

LLM_ENTRY_POINT = "complete"
LLM_TASK = "eval_judge"


def _part_of_node(ctx: Context) -> dict[str, str]:
    return {node.id: part for part, node in ctx.inputs.nodes()}


def _orders(ctx: Context) -> dict[str, tuple[int, int]]:
    """node id -> (order, max order in its part), for the position stratum."""
    per_part: dict[str, int] = {}
    orders: dict[str, tuple[str, int]] = {}
    for part, node in ctx.inputs.nodes():
        per_part[part] = max(per_part.get(part, 0), node.order)
        orders[node.id] = (part, node.order)
    return {nid: (order, per_part[part]) for nid, (part, order) in orders.items()}


def term_population(ctx: Context) -> list[dict[str, Any]]:
    if ctx.inputs.term_uses is None:
        return []
    part_of = _part_of_node(ctx)
    orders = _orders(ctx)
    by_id = ctx.inputs.nodes_by_id()
    out = []
    for u in ctx.inputs.term_uses:
        if u.status != "confident":
            continue
        order, total = orders.get(u.node_id, (0, 0))
        node = by_id.get(u.node_id)
        surface = None
        if node is not None:
            field = node.text if (node.text and u.char_span[1] <= len(node.text)) else node.title
            if field:
                surface = field[u.char_span[0]:u.char_span[1]]
        out.append({
            "kind": "term_use", "term": u.term, "node_id": u.node_id,
            "path": node.path if node else None,
            "char_span": list(u.char_span), "surface": surface,
            "sentence": (node.text if node and node.text else (node.title if node else None)),
            "part": part_of.get(u.node_id, "unknown"),
            "term_word_count": word_count_bucket(u.term),
            "position": position_bucket(order, total),
        })
    return out


def ref_population(ctx: Context) -> list[dict[str, Any]]:
    """Deterministically resolved references, the refs half of layer 4."""
    part_of = _part_of_node(ctx)
    by_path = ctx.inputs.nodes_by_path()
    orders = _orders(ctx)
    out = []
    for part in sorted(ctx.inputs.refs):
        for r in ctx.inputs.refs[part]:
            if r.resolver not in ("grammar", "scope") or not r.target_path:
                continue
            parent_path = r.path.rsplit("/ref@", 1)[0]
            parent = by_path.get(parent_path)
            order, total = orders.get(parent.id, (0, 0)) if parent else (0, 0)
            out.append({
                "kind": "reference", "path": r.path, "text": r.text,
                "ref_kind": r.ref_kind, "resolver": r.resolver,
                "target_path": r.target_path,
                "sentence": parent.text if parent else None,
                "part": part_of.get(parent.id, part) if parent else part,
                "term_word_count": word_count_bucket(r.text or ""),
                "position": position_bucket(order, total),
            })
    return out


def _stratifier(strata: list[str]):
    def key(item: dict[str, Any]) -> tuple:
        return tuple(str(item.get(s, "unknown")) for s in strata)
    return key


def _clip(text: Optional[str], limit: int) -> str:
    """Truncate for the prompt, marking it. A checker shown a sentence cut off
    mid-clause with no marker may call a correct decision wrong because the
    evidence appears to be missing."""
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " […truncated]"


def _run_checker(items: list[dict[str, Any]]) -> tuple[Optional[list[dict]], str]:
    """Ask the independent checker. Returns (verdicts, note)."""
    try:
        from pipeline import llm                          # noqa: PLC0415
    except Exception:                                     # noqa: BLE001
        return None, ("audit runner pending llm.py: pipeline/llm.py is not present, so the "
                      "sample was drawn but not checked")
    fn = getattr(llm, LLM_ENTRY_POINT, None)
    if not callable(fn):
        return None, (f"pipeline.llm exists but exposes no callable "
                      f"{LLM_ENTRY_POINT}(task, prompt); audit not run")
    prompt = ("You are auditing a legal-document pipeline's confident decisions. "
              "For each item say whether the decision is correct. Reply with a JSON "
              "array of objects {\"i\": <index>, \"agree\": true|false, "
              "\"why\": \"<short reason>\"} and nothing else.\n\n"
              + json.dumps([{"i": i, **{k: v for k, v in item.items() if k != "sentence"},
                             "sentence": _clip(item.get("sentence"), 400)}
                            for i, item in enumerate(items)], indent=1))
    try:
        raw = fn(LLM_TASK, prompt)
        verdicts = json.loads(raw)
        if not isinstance(verdicts, list):
            raise ValueError("checker did not return a JSON array")
    except Exception as exc:                              # noqa: BLE001
        return None, f"checker call failed, audit not scored: {type(exc).__name__}: {exc}"
    return verdicts, "checked by pipeline.llm"


def build(ctx: Context) -> Section:
    s = Section("stratified_audit")
    strata = list(config.AUDIT.get("strata", []))
    size = int(config.AUDIT.get("confident_term_sample_size", 0))
    s.data["config"] = {"strata": strata, "confident_term_sample_size": size,
                        "reference_sample_size": size,
                        "reference_sample_size_note": "config.py has no separate knob for "
                                                      "the reference half of layer 4; it "
                                                      "reuses confident_term_sample_size"}
    s.metrics["stratified_audit_agreement"] = None

    populations = {"confident_term_uses": term_population(ctx),
                   "deterministically_resolved_references": ref_population(ctx)}
    if not any(populations.values()):
        s.status = NO_DATA
        s.reason = ("nothing to audit: no confident term uses and no deterministically "
                    "resolved references in scope")
        s.line(f"_{s.reason}_")
        return s

    samples: dict[str, Any] = {}
    drawn: list[dict[str, Any]] = []
    for name, population in populations.items():
        result = stratified_sample(population, _stratifier(strata), size, strata,
                                   seed_material=f"{name}|{ctx.run}")
        samples[name] = result.as_dict()
        drawn.extend(population[i] for i in result.indices)

    verdicts, note = (None, "--no-llm: checker not called") if ctx.options.get("no_llm") \
        else _run_checker(drawn)

    s.data["samples"] = samples
    s.data["sample_items"] = drawn[:LIST_CAP]
    s.data["sample_items_not_listed"] = max(0, len(drawn) - LIST_CAP)
    s.data["checker"] = {"note": note, "entry_point": f"pipeline.llm.{LLM_ENTRY_POINT}",
                         "task": LLM_TASK}

    if verdicts is None:
        s.status = NO_DATA
        s.reason = note
        s.line(f"**{note}**")
        s.line()
        s.line(f"Sample drawn and stored, {len(drawn)} item(s) across "
               f"{len(populations)} population(s), stratified by "
               f"{', '.join(strata)}. Agreement is not reported, because it was not "
               f"measured; the gate `stratified_audit_agreement_min` records "
               f"skipped_no_data.")
        for name, result in samples.items():
            s.line()
            s.line(f"**{name}**: {result['drawn_sample_size']} of "
                   f"{result['population_size']}, seed `{result['seed']}`")
            s.table([" / ".join(strata), "population", "sampled"],
                    [[c["stratum"].replace(" | ", " / "), c["population"], c["sampled"]]
                     for c in result["cells"]])
        return s

    agreed = [v for v in verdicts if v.get("agree") is True]
    disagreed = [v for v in verdicts if v.get("agree") is False]
    # A verdict that is neither true nor false is a checker failure, not an
    # agreement and not a disagreement. Counted explicitly: 38 unusable verdicts
    # out of 40 must not read as a green 2/2, which is what dropping them did.
    unusable = [v for v in verdicts
                if not isinstance(v, dict) or v.get("agree") not in (True, False)
                or not isinstance(v.get("i"), int) or not 0 <= v["i"] < len(drawn)]
    agreement = Rate(len(agreed), len(agreed) + len(disagreed))
    s.status = MEASURED if (agreement.has_data and not unusable) else PARTIAL
    if unusable:
        s.reason = (f"{len(unusable)} of {len(verdicts)} checker verdict(s) were "
                    f"unusable (missing or non-boolean 'agree', or an out-of-range "
                    f"item index) and are counted in neither side of the rate")
    s.metrics["stratified_audit_agreement"] = agreement
    s.data["agreement"] = agreement.as_dict()
    s.data["checker_verdicts"] = {
        "returned": len(verdicts), "agreed": len(agreed),
        "disagreed": len(disagreed), "unusable": len(unusable),
        "scored": Rate(len(agreed) + len(disagreed), len(drawn)).as_dict(),
    }
    s.data["disagreements"] = [
        {**{k: v for k, v in drawn[d["i"]].items() if k != "sentence"},
         "why": d.get("why")}
        for d in disagreed if isinstance(d.get("i"), int) and d["i"] < len(drawn)][:LIST_CAP]
    s.line(f"Drew **{len(drawn)}** item(s), the checker returned "
           f"**{len(verdicts)}** verdict(s), of which **{len(unusable)}** were "
           f"unusable. Agreement over the usable ones: **{agreement}**.")
    s.line()
    s.table(["item", "why the checker disagreed"],
            [[d.get("path") or d.get("term"), d.get("why")] for d in s.data["disagreements"]])
    return s
