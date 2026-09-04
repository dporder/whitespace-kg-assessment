"""Stage 5 CLI. `python -m pipeline.concepts` from the repo root.

Reads stage 2 trees and writes:

    output/<run>/concepts.json            list[Concept], what stage 7 and 8 read
    output/<run>/concepts/scan.json       per-unit call state, prompts, raw proposals
    output/<run>/concepts/resolution.json the merge log and the term collisions
    output/<run>/concepts/summary.json    counts before and after resolution

Exit 0 on success, 2 on an invariant violation, 1 on failure. With no
`pipeline/llm.py` the scan is queued rather than run, `concepts.json` is written
empty, and the summary says plainly that nothing was scanned; one rerun with the
client present completes it from the replay cache upward.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Optional

import config
from pipeline.concepts import resolve as resolve_mod
from pipeline.concepts import scan as scan_mod
from pipeline.embeddings.client import Embedder
from pipeline.schemas import Concept
from pipeline.vocabulary import llmio, treeio

STAGE = "concepts"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m pipeline.concepts",
        description="Stage 5, the concept tier (handover/SPEC.md 2.4).")
    p.add_argument("--run", metavar="ID")
    p.add_argument("--input", choices=["auto", "output", "fixtures"], default="auto")
    p.add_argument("--parts", nargs="*", metavar="ID",
                   help="scan only these parts. The scan is the pipeline's "
                        "spend bottleneck, so a run may deliberately sample it; "
                        "the parts scanned are recorded in scope.json so a part "
                        "with no concepts because it was out of scope stays "
                        "distinguishable from one the scan missed")
    p.add_argument("--all", dest="scan_all", action="store_true",
                   help="scan every part present. The default, stated "
                        "explicitly so a sampled run reads as a choice")
    p.add_argument("--batch", metavar="ID",
                   help=f"limit to a batch's part. one of {sorted(config.BATCHES)}")
    p.add_argument("--output-dir", type=Path, default=config.OUTPUT)
    p.add_argument("--fixtures-dir", type=Path, default=config.ROOT / "fixtures")
    p.add_argument("--no-llm", action="store_true",
                   help="build the scan prompts but call no model")
    p.add_argument("--no-embed", action="store_true",
                   help="resolve near duplicates by the lexical fallback only")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def run(args: argparse.Namespace) -> int:
    from pipeline.embeddings.__main__ import resolve_source
    source, source_root, run_id, run_dir, wanted = resolve_source(args)
    output_root: Path = args.output_dir
    present = treeio.discover_parts(source_root)
    if args.scan_all:
        wanted = present
    skipped = [p for p in present if p not in wanted]
    trees = treeio.load_trees(source, source_root, run_id, wanted)
    # Two scopes, as in stage 4. The scan runs over the parts in scope, but the
    # declared vocabulary the collision guard checks against is document-wide,
    # so it is derived from every tree present. Deriving it from the scanned
    # parts alone silently disarms the guard on a sampled run: this document
    # keeps all 259 declared terms in Joint Schedule 1, so sampling the two
    # clause parts took the real collision count from 14 to 0 and would have
    # minted concepts that a declared Term already owns.
    vocabulary_trees = (trees if sorted(wanted) == sorted(present)
                        else treeio.load_trees(source, source_root, run_id, present))

    llm = llmio.runner(STAGE, run_dir, output_root, enabled=not args.no_llm)
    results = scan_mod.scan(trees, llm)
    llm.flush_log()

    proposed = [c for r in results for c in r.proposed]
    embedder = None if args.no_embed else Embedder(output_root=output_root)
    resolution = resolve_mod.resolve(proposed, vocabulary_trees, embedder)

    by_id = trees.by_id()
    violations = []
    for concept in resolution.concepts:
        if not concept.member_node_ids:
            violations.append({"check": "concept_has_members", "id": concept.id,
                               "detail": concept.label})
        for nid in concept.member_node_ids:
            if nid not in by_id:
                violations.append({"check": "member_node_exists", "id": concept.id,
                                   "detail": f"{concept.label} claims {nid}"})
        if not concept.llm_derived:
            violations.append({"check": "concepts_are_llm_derived", "id": concept.id,
                               "detail": concept.label})
    minted_ids = {c.id for c in resolution.concepts}
    for concept in resolution.concepts:
        for relation in concept.relations:
            if relation.dst not in minted_ids:
                violations.append({
                    "check": "concept_relation_targets_a_minted_concept",
                    "id": concept.id,
                    "detail": f"{concept.label} -{relation.label}-> {relation.dst}"})

    # `concepts.json` stays a bare JSON list, because that is what SPEC 3 names
    # and what the stage 8 loader pins (`[Concept.model_validate(x) for x in d]`);
    # wrapping it in an object to carry metadata would make every record fail to
    # load. So the scope rides on each record instead, where a reader of this one
    # file sees the sample without opening another, and where the frozen model
    # ignores it as an unknown field rather than rejecting it. `scope.json` holds
    # the same facts once, and is the copy stage 8 should read.
    dump(run_dir / "concepts.json",
         [{**c.model_dump(exclude_none=True),
           "scanned_parts": sorted(trees.parts),
           "skipped_parts": sorted(skipped)}
          for c in resolution.concepts])

    scan_states: dict[str, int] = {}
    for result in results:
        scan_states[result.state or "unknown"] = \
            scan_states.get(result.state or "unknown", 0) + 1
    dump(run_dir / "concepts" / "scan.json", {
        "task": scan_mod.TASK, "model": config.MODELS.get(scan_mod.TASK),
        "prompt_version": scan_mod.PROMPT_VERSION,
        "units_scanned": len(results), "by_state": dict(sorted(scan_states.items())),
        "note": ("the scan unit is a part or a top-level clause with its full "
                 "derived subtree text; the same units stage 8 measures coverage "
                 "over"),
        "units": [{
            "part": r.unit.part, "path": r.unit.path, "kind": r.unit.node.kind,
            "state": r.state, "note": r.note, "parse_error": r.parse_error,
            "dropped_paths": r.dropped_paths,
            "proposed": [{"label": c.label, "confidence": c.confidence,
                          "provisions": c.member_paths,
                          "relations": c.relations} for c in r.proposed],
            "prompt": r.prompt,
        } for r in results],
    })
    dump(run_dir / "concepts" / "resolution.json", resolution.as_dict())

    # The scan is the pipeline's spend bottleneck, so a run may sample it. That
    # makes "this part has no concepts" ambiguous between *not scanned* and
    # *scanned and found nothing*, and stage 8 measures coverage over every unit
    # in every loaded tree. Recording the scope is what keeps the two apart.
    # `concepts.json` stays a bare `list[Concept]`, because that is the shape
    # SPEC 2.4 and the stage 8 loader both pin, so the scope travels beside it.
    scope = {
        "scanned_parts": sorted(trees.parts),
        "skipped_parts": sorted(skipped),
        "parts_present_in_the_run": sorted(present),
        "scan_units": len(results),
        "vocabulary_derived_from_parts": sorted(vocabulary_trees.parts),
        "note": ("a part in skipped_parts has no concepts because it was never "
                 "scanned, not because the scan found none. Stage 8's coverage "
                 "denominator should be the scanned parts, not every loaded "
                 "tree, or a sampled run reads as a failed one."),
    }
    dump(run_dir / "concepts" / "scope.json", scope)

    summary = {
        "stage": STAGE, "run": run_id, "input_source": source,
        "parts": sorted(trees.parts),
        "scope": scope,
        "scan": {"units": len(results), "by_state": dict(sorted(scan_states.items())),
                 "task": scan_mod.TASK, "model": config.MODELS.get(scan_mod.TASK),
                 "units_with_no_concept": sum(1 for r in results if not r.proposed),
                 "dropped_invented_paths": sum(len(r.dropped_paths) for r in results)},
        "concepts": {
            "proposed_before_resolution": len(proposed),
            "minted_after_resolution": len(resolution.concepts),
            "not_minted_term_collision": len(resolution.collisions),
            "merged_away": len(resolution.merges),
            "resolution_method": resolution.method,
            "merge_threshold": config.CONCEPT_MERGE_COSINE,
            "note": resolution.note,
            "relations": sum(len(c.relations) for c in resolution.concepts),
        },
        "associated_term": ("not computed here: it joins stage 4 and stage 5 "
                            "output, so SPEC 2.4 puts it in stage 7"),
        "llm": llm.summary(),
        "violations": len(violations),
    }
    dump(run_dir / "concepts" / "summary.json", summary)
    if violations:
        dump(run_dir / "concepts" / "violations.json",
             {"count": len(violations), "violations": violations})

    [Concept.model_validate(json.loads(c.model_dump_json()))
     for c in resolution.concepts]

    if not args.quiet:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\nwrote {run_dir / 'concepts.json'} and {run_dir / 'concepts'}")
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
