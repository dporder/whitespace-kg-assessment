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

import inspect
import json
import re
from pathlib import Path
from typing import Any, Optional

import config
from pipeline.eval.context import (AUDIT_BATCH_SIZE, AUDIT_TOKEN_OVERHEAD,
                                   AUDIT_TOKENS_PER_ITEM, Context, LIST_CAP)
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


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def tolerant_json(raw: str) -> Any:
    """JSON out of a model's text: fences stripped, then the outermost value.

    Only used when `pipeline.llm` exposes no parser of its own. A model told to
    answer in JSON still wraps it in a fence or opens with a sentence often
    enough that `json.loads` on the raw text is not a reasonable contract, and
    treating that as a failed audit throws away a perfectly good answer.
    """
    text = (raw or "").strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except Exception:                                     # noqa: BLE001
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except Exception:                             # noqa: BLE001
                continue
    raise ValueError(f"no JSON value in a {len(raw or '')}-character response")


def _prompt_for(batch: list[dict[str, Any]], first: int) -> str:
    return ("You are auditing a legal-document pipeline's confident decisions. "
            "For each item say whether the decision is correct. Reply with a JSON "
            "array of objects {\"i\": <index>, \"agree\": true|false, "
            "\"why\": \"<short reason>\"} and nothing else: no prose before or "
            "after, no markdown fences. Use the \"i\" value given with each "
            "item.\n\n"
            + json.dumps([{"i": first + n,
                           **{k: v for k, v in item.items() if k != "sentence"},
                           "sentence": _clip(item.get("sentence"), 400)}
                          for n, item in enumerate(batch)], indent=1))


def _call_llm(llm: Any, prompt: str, max_tokens: int) -> str:
    """`complete(task, prompt)` is the pinned contract; the keyword extras are
    used only when this build of llm.py actually takes them."""
    kwargs: dict[str, Any] = {}
    try:
        params = inspect.signature(llm.complete).parameters
    except (TypeError, ValueError):                       # pragma: no cover
        params = {}
    # A **kwargs signature accepts everything; filtering against one would strip
    # the whole payload.
    takes_anything = any(p.kind is inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
    if (takes_anything or "system" in params) and getattr(llm, "JSON_SYSTEM", None):
        kwargs["system"] = llm.JSON_SYSTEM
    if takes_anything or "max_tokens" in params:
        kwargs["max_tokens"] = max_tokens
    try:
        return llm.complete(LLM_TASK, prompt, **kwargs)
    except TypeError:
        # An llm.py that takes only the pinned (task, prompt) after all.
        return llm.complete(LLM_TASK, prompt)


def _save_raw(eval_dir: Optional[Path], index: int, raw: str) -> Optional[str]:
    """An unparseable reply is evidence. Put it where someone can read it."""
    if eval_dir is None:
        return None
    try:
        d = eval_dir / "audit_raw"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"batch-{index}.txt"
        path.write_text(raw or "")
        return str(path)
    except Exception:                                     # noqa: BLE001
        return None


def _run_checker(items: list[dict[str, Any]],
                 eval_dir: Optional[Path] = None) -> tuple[Optional[list[dict]], str, dict]:
    """Ask the independent checker, in batches. Returns (verdicts, note, diagnostics).

    Batched on purpose. One unparseable reply used to abandon the whole audit;
    now it costs its own batch and the rest still scores. `verdicts` carries the
    absolute item index in "i", so a failed batch leaves a hole rather than
    shifting everything after it.
    """
    diagnostics: dict[str, Any] = {"batches": [], "batch_size": AUDIT_BATCH_SIZE}
    try:
        from pipeline import llm                          # noqa: PLC0415
    except Exception:                                     # noqa: BLE001
        return None, ("audit runner pending llm.py: pipeline/llm.py is not present, so the "
                      "sample was drawn but not checked"), diagnostics
    if not callable(getattr(llm, LLM_ENTRY_POINT, None)):
        return None, (f"pipeline.llm exists but exposes no callable "
                      f"{LLM_ENTRY_POINT}(task, prompt); audit not run"), diagnostics

    # llm.py's own parser when it has one: fences and leading prose are its
    # problem to know about, not this section's guess at its behaviour.
    parser = getattr(llm, "parse_json", None)
    diagnostics["parser"] = ("pipeline.llm.parse_json" if callable(parser)
                             else "pipeline/eval tolerant_json fallback")
    parse = parser if callable(parser) else tolerant_json
    unavailable = getattr(llm, "LLMUnavailable", None)

    # Put the judge's calls in this run's log. Without this llm.py logs to its
    # own default run, so a judge that ran leaves nothing under the run being
    # reported on and nobody can tell whether the call was made at all. That is
    # what an empty output/<run>/llm_log looked like from the outside.
    if eval_dir is not None and callable(getattr(llm, "set_run_dir", None)):
        try:
            diagnostics["llm_log_dir"] = str(llm.set_run_dir(eval_dir.parent))
        except Exception as exc:                          # noqa: BLE001
            diagnostics["llm_log_dir"] = f"could not be set: {type(exc).__name__}: {exc}"

    verdicts: list[dict] = []
    scored_any = False
    for start in range(0, len(items), AUDIT_BATCH_SIZE):
        batch = items[start:start + AUDIT_BATCH_SIZE]
        index = start // AUDIT_BATCH_SIZE
        budget = AUDIT_TOKEN_OVERHEAD + AUDIT_TOKENS_PER_ITEM * len(batch)
        record: dict[str, Any] = {"batch": index, "items": len(batch),
                                  "first_item": start}
        raw = None
        try:
            raw = _call_llm(llm, _prompt_for(batch, start), budget)
            parsed = parse(raw)
            # A lone verdict object is a one-item list. Worth accepting on its
            # own merits, and it also absorbs a parser that looks for an object
            # before an array: llm.py's parse_json returns the inner dict for a
            # single-element array wrapped in prose, which would otherwise lose
            # the last batch of every sample whose size is not a round multiple.
            if isinstance(parsed, dict) and "agree" in parsed:
                parsed = [parsed]
            if not isinstance(parsed, list):
                raise ValueError(f"checker returned {type(parsed).__name__}, not a list")
            verdicts.extend(v for v in parsed if isinstance(v, dict))
            record["state"] = "scored"
            record["verdicts"] = len(parsed)
            scored_any = True
        except Exception as exc:                          # noqa: BLE001
            # The breaker case is different in kind: no key, or the workspace id
            # is missing, so every later call would fail the same way. Stop
            # calling, keep whatever scored, and say why.
            broken = unavailable is not None and isinstance(exc, unavailable)
            record["state"] = "unavailable" if broken else "unparseable"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["raw_response_chars"] = len(raw) if raw is not None else None
            record["raw_response_saved_to"] = _save_raw(eval_dir, index, raw or "")
            diagnostics["batches"].append(record)
            if broken:
                diagnostics["stopped_early"] = record["error"]
                break
            continue
        diagnostics["batches"].append(record)

    failed = [b for b in diagnostics["batches"] if b["state"] != "scored"]
    diagnostics["batches_failed"] = len(failed)
    diagnostics["items_in_failed_batches"] = sum(b["items"] for b in failed)
    if not scored_any:
        first = failed[0]["error"] if failed else "no batch was attempted"
        return None, (f"checker returned nothing scorable: {first}"
                      + (f" (raw reply saved to {failed[0]['raw_response_saved_to']})"
                         if failed and failed[0].get("raw_response_saved_to") else "")
                      ), diagnostics
    note = "checked by pipeline.llm"
    if failed:
        note += (f"; {len(failed)} of {len(diagnostics['batches'])} batch(es) returned "
                 f"nothing scorable and are counted, not dropped")
    return verdicts, note, diagnostics


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

    if ctx.options.get("no_llm"):
        verdicts, note, diagnostics = None, "--no-llm: checker not called", {}
    else:
        verdicts, note, diagnostics = _run_checker(drawn, ctx.eval_dir)

    s.data["samples"] = samples
    s.data["sample_items"] = drawn[:LIST_CAP]
    s.data["sample_items_not_listed"] = max(0, len(drawn) - LIST_CAP)
    s.data["checker"] = {"note": note, "entry_point": f"pipeline.llm.{LLM_ENTRY_POINT}",
                         "task": LLM_TASK, **diagnostics}

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

    # One verdict per item. A checker that answers the same index twice, which
    # a re-sent batch or an over-eager reply will do, must not get two votes:
    # the first stands and the rest are counted as duplicates. Without this the
    # scored count can exceed the sample and "never scored" goes negative.
    usable: dict[int, dict] = {}
    unusable: list[dict] = []
    duplicates = 0
    for v in verdicts:
        ok = (isinstance(v, dict) and v.get("agree") in (True, False)
              and isinstance(v.get("i"), int) and 0 <= v["i"] < len(drawn))
        if not ok:
            unusable.append(v)
        elif v["i"] in usable:
            duplicates += 1
        else:
            usable[v["i"]] = v
    agreed = [v for v in usable.values() if v["agree"] is True]
    disagreed = [v for v in usable.values() if v["agree"] is False]
    agreement = Rate(len(agreed), len(agreed) + len(disagreed))
    unscored = len(drawn) - (len(agreed) + len(disagreed))
    s.status = (MEASURED if (agreement.has_data and not unusable and not unscored)
                else PARTIAL)
    reasons = []
    if unusable:
        reasons.append(f"{len(unusable)} of {len(verdicts)} checker verdict(s) were "
                       f"unusable (missing or non-boolean 'agree', or an out-of-range "
                       f"item index) and are counted in neither side of the rate")
    if diagnostics.get("batches_failed"):
        reasons.append(f"{diagnostics['batches_failed']} batch(es) covering "
                       f"{diagnostics['items_in_failed_batches']} item(s) returned "
                       f"nothing scorable; the raw replies are saved beside this report")
    if diagnostics.get("stopped_early"):
        reasons.append(f"stopped early: {diagnostics['stopped_early']}")
    s.reason = "; ".join(reasons) or None
    s.metrics["stratified_audit_agreement"] = agreement
    s.data["agreement"] = agreement.as_dict()
    s.data["checker_verdicts"] = {
        "drawn": len(drawn), "returned": len(verdicts), "agreed": len(agreed),
        "disagreed": len(disagreed), "unusable": len(unusable),
        "duplicate_verdicts_ignored": duplicates, "never_scored": unscored,
        "scored": Rate(len(agreed) + len(disagreed), len(drawn)).as_dict(),
    }
    s.data["disagreements"] = [
        {**{k: v for k, v in drawn[d["i"]].items() if k != "sentence"},
         "why": d.get("why")}
        for d in disagreed if isinstance(d.get("i"), int) and d["i"] < len(drawn)][:LIST_CAP]
    s.line(f"Drew **{len(drawn)}** item(s) and scored "
           f"**{Rate(len(agreed) + len(disagreed), len(drawn))}** of them. "
           f"Agreement over the scored ones: **{agreement}**.")
    s.bullet(f"checker: {note}")
    s.bullet(f"JSON parser: {diagnostics.get('parser', 'unknown')}")
    if unusable or unscored:
        s.bullet(f"{len(unusable)} unusable verdict(s), {unscored} item(s) never "
                 f"scored. Neither is counted as agreement.")
    failed_batches = [b for b in diagnostics.get("batches", []) if b["state"] != "scored"]
    if failed_batches:
        s.line()
        s.line("**Batches that returned nothing scorable**")
        s.table(["batch", "items", "state", "error", "raw reply"],
                [[b["batch"], b["items"], b["state"], b.get("error"),
                  b.get("raw_response_saved_to") or "not saved"]
                 for b in failed_batches[:LIST_CAP]])
    s.line()
    s.table(["item", "why the checker disagreed"],
            [[d.get("path") or d.get("term"), d.get("why")] for d in s.data["disagreements"]])
    return s
