"""Stage 6 CLI. `python -m pipeline.embeddings` from the repo root.

Reads stage 2 trees and writes `output/<run>/embeddings/`:

    plan.json       every planned embedding, its altitude and why  (deterministic)
    records.json    list[EmbeddingRecord], only for items that have a vector
    index.json      node id -> {level, vector_ref}, the retrieval index
    pending.json    what has no vector or no summary yet, and why
    summary.json    counts, availability, and what this run could not do

The plan is a pure function of the trees and config and runs in full with no
key and no model. Summaries need `pipeline/llm.py`; vectors need an OpenAI key
with credit. Where either is missing the item lands in `pending.json` and no
record is written, because a record whose `vector_ref` points at a file that
does not exist would be a lie about what the index holds.

Exit 0 on success, 2 on an invariant violation, 1 on failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Optional

import config
from pipeline.schemas import EmbeddingRecord
from pipeline.embeddings import client as client_mod
from pipeline.embeddings import plan as plan_mod
from pipeline.embeddings import summaries as summaries_mod
from pipeline.embeddings.tokens import ESTIMATOR
from pipeline.vocabulary import llmio, treeio

STAGE = "embeddings"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m pipeline.embeddings",
        description="Stage 6, embeddings and summaries (handover/SPEC.md 2.4).")
    p.add_argument("--run", metavar="ID")
    p.add_argument("--input", choices=["auto", "output", "fixtures"], default="auto")
    p.add_argument("--parts", nargs="*", metavar="ID")
    p.add_argument("--batch", metavar="ID",
                   help=f"limit to a batch's part. one of {sorted(config.BATCHES)}")
    p.add_argument("--output-dir", type=Path, default=config.OUTPUT)
    p.add_argument("--fixtures-dir", type=Path, default=config.ROOT / "fixtures")
    p.add_argument("--leaf-window", dest="leaf_window", action="store_true",
                   default=None,
                   help="the A/B variant: embed each leaf with its previous and "
                        "next sibling, REPLACING leaf_text. Default is "
                        f"config.LEAF_WINDOW_EMBEDDING ({config.LEAF_WINDOW_EMBEDDING})")
    p.add_argument("--no-leaf-window", dest="leaf_window", action="store_false")
    p.add_argument("--no-llm", action="store_true",
                   help="plan the summaries but generate none")
    p.add_argument("--no-embed", action="store_true",
                   help="build the plan and the prompts but call no embedding API")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def resolve_source(args: argparse.Namespace) -> tuple[str, Path, str, Path, list[str]]:
    output_root: Path = args.output_dir
    run_id = args.run or treeio.newest_run(output_root) or "dev"
    run_dir = output_root / run_id
    source = args.input
    if source == "auto":
        source = "output" if treeio.has_trees(run_dir) else "fixtures"
    source_root = run_dir if source == "output" else args.fixtures_dir
    present = treeio.discover_parts(source_root)
    wanted = args.parts if args.parts else present
    if args.batch:
        if args.batch not in config.BATCHES:
            raise SystemExit(f"unknown batch {args.batch!r}; "
                             f"config.BATCHES has {sorted(config.BATCHES)}")
        wanted = [p for p in wanted if p == config.BATCHES[args.batch]["part"]]
    missing = [p for p in wanted if p not in present]
    if missing:
        raise SystemExit(f"no stage 2 tree for {missing} under {source_root / 'tree'}")
    if not wanted:
        raise SystemExit(f"no stage 2 trees found under {source_root / 'tree'}")
    return source, source_root, run_id, run_dir, wanted


def run(args: argparse.Namespace) -> int:
    source, source_root, run_id, run_dir, wanted = resolve_source(args)
    output_root: Path = args.output_dir
    trees = treeio.load_trees(source, source_root, run_id, wanted)

    leaf_window = (config.LEAF_WINDOW_EMBEDDING if args.leaf_window is None
                   else args.leaf_window)
    plan = plan_mod.build(trees, config.SUBTREE_EMBED_TOKEN_BUDGET, leaf_window)

    # -- summaries -----------------------------------------------------------
    llm = llmio.runner(STAGE, run_dir, output_root, enabled=not args.no_llm)
    owed = [i for i in plan.items if i.needs_summary]
    outcomes = summaries_mod.generate(owed, llm)
    llm.flush_log()
    summary_states: dict[str, int] = {}
    for outcome in outcomes:
        summary_states[outcome.state] = summary_states.get(outcome.state, 0) + 1

    # -- vectors -------------------------------------------------------------
    embedder = client_mod.Embedder(output_root=output_root, enabled=not args.no_embed)
    ready = [i for i in plan.items if i.text.strip()]
    embedded = embedder.embed([i.text for i in ready])

    records: list[EmbeddingRecord] = []
    index: dict[str, dict] = {}
    pending: list[dict] = []
    for item in plan.items:
        if item.needs_summary and not item.text.strip():
            outcome = next((o for o in outcomes if o.item is item), None)
            pending.append({**item.as_dict(), "blocked_on": "summary",
                            "state": outcome.state if outcome else "unknown",
                            "note": outcome.note if outcome else ""})
            continue
        ref = embedded.vectors.get(item.text)
        if ref is None:
            pending.append({**item.as_dict(), "blocked_on": "vector",
                            "state": embedded.missing.get(item.text, "unknown"),
                            "note": embedded.note})
            continue
        record = item.record(ref)
        records.append(record)
        index[item.node_id] = {"level": record.level, "vector_ref": ref,
                               "dimensions": embedded.dims.get(item.text),
                               "llm_derived": record.llm_derived,
                               "path": item.path}

    embed_dir = run_dir / "embeddings"
    dump(embed_dir / "plan.json", {
        "token_estimator": ESTIMATOR,
        "subtree_token_budget": config.SUBTREE_EMBED_TOKEN_BUDGET,
        "leaf_window_embedding": leaf_window,
        "levels": {"by_level": plan.by_level(), "total": len(plan.items)},
        "note": ("leaf_window replaces leaf_text when the flag is on; the two are "
                 "never stored for one leaf"),
        "items": [i.as_dict() for i in plan.items],
        "skipped": plan.skipped,
    })
    dump(embed_dir / "records.json",
         [r.model_dump(exclude_none=True) for r in records])
    dump(embed_dir / "index.json", {
        "model": embedder.model, "keyed_by": "node_id",
        "vector_store": f"embeddings_cache/{embedder.model}/",
        "note": ("vectors live under output/, never on graph nodes, so "
                 "re-embedding on a new model never rewrites the graph"),
        "entries": dict(sorted(index.items())),
    })
    dump(embed_dir / "pending.json", {
        "count": len(pending),
        "note": ("nothing here has a vector or a summary yet. No EmbeddingRecord "
                 "was written for these, because a record whose vector_ref points "
                 "at a file that does not exist would misdescribe the index. One "
                 "rerun completes them."),
        "items": pending,
    })

    violations = []
    for record in records:
        if not (output_root / record.vector_ref).exists():
            violations.append({"check": "vector_ref_exists", "node_id": record.node_id,
                               "detail": record.vector_ref})
        if record.level == "summary" and not record.llm_derived:
            violations.append({"check": "summary_is_llm_derived",
                               "node_id": record.node_id, "detail": record.level})
    levels_per_node: dict[str, set[str]] = {}
    for record in records:
        levels_per_node.setdefault(record.node_id, set()).add(record.level)
    for node_id, levels in levels_per_node.items():
        if {"leaf_text", "leaf_window"} <= levels:
            violations.append({"check": "one_leaf_variant_only", "node_id": node_id,
                               "detail": sorted(levels)})

    summary = {
        "stage": STAGE, "run": run_id, "input_source": source,
        "parts": sorted(trees.parts),
        "plan": {"total": len(plan.items), "by_level": plan.by_level(),
                 "skipped": len(plan.skipped),
                 "token_estimator": ESTIMATOR,
                 "subtree_token_budget": config.SUBTREE_EMBED_TOKEN_BUDGET,
                 "leaf_window_embedding": leaf_window},
        "summaries": {"owed": len(owed), "generated": sum(1 for o in outcomes if o.text),
                      "by_state": dict(sorted(summary_states.items())),
                      "task": summaries_mod.TASK,
                      "model": config.MODELS.get(summaries_mod.TASK)},
        "vectors": {"records_written": len(records), "cache_hits": embedded.cache_hits,
                    "newly_embedded": embedded.embedded,
                    "api_calls": embedded.api_calls, "note": embedded.note,
                    **embedder.availability()},
        "llm": llm.summary(),
        "pending": {"total": len(pending),
                    "by_reason": {r: sum(1 for p in pending if p["state"] == r)
                                  for r in sorted({p["state"] for p in pending})}},
        "violations": len(violations),
    }
    dump(embed_dir / "summary.json", summary)
    if violations:
        dump(embed_dir / "violations.json",
             {"count": len(violations), "violations": violations})

    if not args.quiet:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\nwrote {embed_dir}")
    return 2 if violations else 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except SystemExit:
        raise
    except Exception:                                      # noqa: BLE001
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
