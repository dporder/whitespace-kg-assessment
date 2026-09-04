"""Stage 4 CLI. `python -m pipeline.vocabulary` from the repo root.

Reads stage 2 trees and writes `output/<run>/vocab/`:

    definition_sites.json      list[DefinitionSite]  (stage 7 and stage 8 read this)
    term_uses.json             list[TermUse]         (stage 7 and stage 8 read this)
    definition_sites_provenance.json  which block minted each term, and the ink defects
    discovery_diff.json        declared against discovered, the three sets
    typo_density.json          the per-section signal and which sections tripped it
    routing.json               the typed ambiguity queues, prompts and verdicts
    audit_sample.json          the stratified sample of confident matches
    summary.json               every count this run derived, plus what it could not do
    violations.json            written only when an invariant fails

Exit 0 on success, 2 on an invariant violation (output is still written), 1 on
failure. The deterministic half of the stage runs with no model and no network;
the routed ambiguity checks degrade to a queued pending marker when
`pipeline/llm.py` is absent.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Optional

import config
from pipeline.schemas import DefinitionSite, TermUse
from pipeline.vocabulary import audit as audit_mod
from pipeline.vocabulary import declared as declared_mod
from pipeline.vocabulary import discovery as discovery_mod
from pipeline.vocabulary import llmio, matching, routing, sites as sites_mod, treeio, typos

STAGE = "vocabulary"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m pipeline.vocabulary",
        description="Stage 4, the vocabulary tier (handover/SPEC.md 2.3).")
    p.add_argument("--run", metavar="ID",
                   help="run id under output/. default: the newest run holding "
                        "stage 2 trees, else 'dev'")
    p.add_argument("--input", choices=["auto", "output", "fixtures"], default="auto",
                   help="where stage 2 trees are read from. auto prefers the run "
                        "directory and falls back to fixtures/")
    p.add_argument("--parts", nargs="*", metavar="ID",
                   help="limit to these part ids. default: every tree present")
    p.add_argument("--batch", metavar="ID",
                   help=f"limit to a batch's part. one of {sorted(config.BATCHES)}")
    p.add_argument("--output-dir", type=Path, default=config.OUTPUT)
    p.add_argument("--fixtures-dir", type=Path, default=config.ROOT / "fixtures")
    p.add_argument("--no-llm", action="store_true",
                   help="build every routing queue and prompt but call no model")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False,
                               sort_keys=False) + "\n")


def _orders(trees: treeio.Trees) -> dict[str, tuple[int, int]]:
    """node id -> (order, max order in its part), for the audit position stratum."""
    per_part: dict[str, int] = {}
    where: dict[str, tuple[str, int]] = {}
    for part, node in trees.nodes():
        per_part[part] = max(per_part.get(part, 0), node.order)
        where[node.id] = (part, node.order)
    return {nid: (order, per_part[part]) for nid, (part, order) in where.items()}


def document_scope_parts(trees: treeio.Trees) -> set[str]:
    return {pid for pid, part in trees.ordered()
            if declared_mod.part_is_document_definitions(part, config.BATCHES)}


def check_invariants(term_uses: list[matching.Match], merged: list[sites_mod.MergedSite],
                     trees: treeio.Trees) -> list[dict]:
    """Every span must reproduce its surface, every site must name a real node.

    A term use whose offsets do not cut out the text they claim is a broken edge
    in the graph, not a metric, so it fails the stage rather than being counted.
    """
    by_id = trees.by_id()
    violations: list[dict] = []
    for m in term_uses:
        node = by_id.get(m.node_id)
        if node is None:
            violations.append({"check": "term_use_node_exists", "path": m.node_path,
                               "detail": f"no node with id {m.node_id}"})
            continue
        value = node.title if m.field_name == "title" else node.text
        if value is None or value[m.span[0]:m.span[1]] != m.surface:
            violations.append({
                "check": "term_use_span_reproduces_surface", "path": m.node_path,
                "detail": f"{m.field_name}[{m.span[0]}:{m.span[1]}] is "
                          f"{(value or '')[m.span[0]:m.span[1]]!r}, expected {m.surface!r}"})
    for site in merged:
        if site.raw.definition_node_id not in by_id:
            violations.append({"check": "definition_node_exists",
                               "path": site.raw.definition_node_path,
                               "detail": f"term {site.term!r} names a definition node "
                                         f"that is not in the trees"})
    return violations


def run(args: argparse.Namespace) -> int:
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
        batch_part = config.BATCHES[args.batch]["part"]
        wanted = [p for p in wanted if p == batch_part]
    missing = [p for p in wanted if p not in present]
    if missing:
        raise SystemExit(f"no stage 2 tree for {missing} under {source_root / 'tree'}")
    if not wanted:
        raise SystemExit(f"no stage 2 trees found under {source_root / 'tree'}")

    trees = treeio.load_trees(source, source_root, run_id, wanted)
    doc_parts = document_scope_parts(trees)

    # -- vocabulary ----------------------------------------------------------
    declared_sites = declared_mod.ingest(trees, config.BATCHES)
    discovered = discovery_mod.discover(trees)
    merged, unattached_aliases = sites_mod.merge(
        declared_sites, discovered.sites, discovered.aliases, doc_parts)

    signal = typos.compute(trees, config.TYPO_DENSITY_THRESHOLD)

    definition_text: dict[str, str] = {}
    by_id = trees.by_id()
    for site in merged:
        node = by_id.get(site.raw.definition_node_id)
        if node is not None and node.text and site.term not in definition_text:
            definition_text[site.term] = node.text

    # -- matching ------------------------------------------------------------
    vocabularies: dict[str, sites_mod.PartVocabulary] = {}
    all_matches: list[matching.Match] = []
    for pid, part in trees.ordered():
        vocab = sites_mod.vocabulary_for(pid, merged)
        vocabularies[pid] = vocab
        all_matches.extend(matching.match_part(
            part, vocab, merged, signal.is_typo_dense, treeio.section_of(part)))

    # -- routing -------------------------------------------------------------
    llm = llmio.runner(STAGE, run_dir, output_root, enabled=not args.no_llm)

    def candidates_for(term: str) -> list[str]:
        return [term]

    queues = routing.route(all_matches, llm,
                           lambda t: definition_text.get(t), candidates_for)
    kept, rejected = routing.apply(all_matches, queues)
    llm.flush_log()

    # -- audit ---------------------------------------------------------------
    sample = audit_mod.draw(kept, _orders(trees), config.AUDIT, run_id)

    # -- write ---------------------------------------------------------------
    vocab_dir = run_dir / "vocab"
    schema_sites = [s.to_schema() for s in merged]
    schema_uses = [m.to_schema() for m in kept]
    dump(vocab_dir / "definition_sites.json",
         [s.model_dump(exclude_none=True, exclude_defaults=True) for s in schema_sites])
    dump(vocab_dir / "term_uses.json",
         [u.model_dump(exclude_none=True, exclude_defaults=True) for u in schema_uses])
    dump(vocab_dir / "definition_sites_provenance.json",
         [s.provenance() for s in merged])
    dump(vocab_dir / "typo_density.json", signal.as_dict())
    dump(vocab_dir / "audit_sample.json", sample)
    dump(vocab_dir / "routing.json", {
        **routing.summarise(queues),
        "llm": llm.summary(),
        "rejected_by_checker": rejected,
        "queues_detail": {kind: {"items": [it.payload | {"i": it.index,
                                                         "ambiguity_kinds": it.match.kinds}
                                           for it in q.items],
                                 "batches": q.batches}
                          for kind, q in sorted(queues.items())},
    })

    declared_terms = sorted({s.term for s in merged if s.source in ("declared", "both")})
    discovered_terms = sorted({s.term for s in merged
                               if s.source in ("discovered", "both")})
    both_terms = sorted(set(declared_terms) & set(discovered_terms))
    dump(vocab_dir / "discovery_diff.json", {
        "declared": {"count": len(declared_terms), "terms": declared_terms},
        "discovered": {"count": len(discovered_terms), "terms": discovered_terms},
        "in_both": {"count": len(both_terms), "terms": both_terms},
        "discovered_not_declared": sorted(set(discovered_terms) - set(declared_terms)),
        "declared_not_discovered": sorted(set(declared_terms) - set(discovered_terms)),
        "note": ("the discovery rule keys on the drafting convention (a quoted "
                 "capitalised phrase followed by a definitional verb, or the "
                 "parenthetical form). A definitions-table row whose value cell "
                 "does not print the verb is declared but not discovered, which "
                 "measures how consistently the drafters followed their own "
                 "convention rather than a blind spot in the scanner."),
        "unattached_alias_candidates": [
            {"alias": a.alias, "phrase": a.phrase, "path": a.node_path, "part": a.part}
            for a in unattached_aliases],
    })

    counts_by_status: dict[str, int] = {}
    counts_by_kind: dict[str, int] = {}
    for m in kept:
        counts_by_status[m.status] = counts_by_status.get(m.status, 0) + 1
        counts_by_kind[m.ambiguity_kind] = counts_by_kind.get(m.ambiguity_kind, 0) + 1

    anomalies = [
        {"term": s.term, "path": s.raw.term_node_path, "anomalies": s.raw.anomalies}
        for s in merged if s.raw.anomalies]
    dump(vocab_dir / "anomalies.json", {
        "note": ("defects in the source ink, recorded beside the term and never "
                 "repaired. A term key is normalised for keying only."),
        "count": len(anomalies), "sites": anomalies})

    violations = check_invariants(kept, merged, trees)
    summary = {
        "stage": STAGE, "run": run_id, "input_source": source,
        "input_root": str(source_root), "parts": sorted(trees.parts),
        "document_scope_parts": sorted(doc_parts),
        "definition_sites": {
            "total": len(merged),
            "by_source": {k: sum(1 for s in merged if s.source == k)
                          for k in ("declared", "discovered", "both")},
            "by_scope": {scope: sum(1 for s in merged if s.scope == scope)
                         for scope in sorted({s.scope for s in merged})},
            "distinct_terms": len({s.term for s in merged}),
            "with_pointer": sum(1 for s in merged if s.raw.pointer),
            "with_aliases": sum(1 for s in merged if s.raw.aliases),
            "aliases": sorted({a for s in merged for a in s.raw.aliases}),
            "duplicate_definitions": sum(1 for s in merged if s.duplicate_of),
            "source_ink_anomalies": len(anomalies),
        },
        "declared_vs_discovered": {
            "declared_terms": len(declared_terms),
            "discovered_terms": len(discovered_terms),
            "in_both": len(both_terms),
            "discovered_not_declared": len(set(discovered_terms) - set(declared_terms)),
            "declared_not_discovered": len(set(declared_terms) - set(discovered_terms)),
        },
        "term_uses": {
            "total": len(kept), "by_status": dict(sorted(counts_by_status.items())),
            "by_ambiguity_kind": dict(sorted(counts_by_kind.items())),
            "by_method": {m: sum(1 for u in kept if u.method == m)
                          for m in sorted({u.method for u in kept})},
            "alias_matches": sum(1 for m in kept if m.is_alias),
            "removed_by_routed_check": len(rejected),
            "on_definition_nodes": sum(
                1 for m in kept
                if m.node_id in {s.raw.definition_node_id for s in merged}),
        },
        "scope": {
            "terms_out_of_scope_per_part": {
                pid: v.suppressed_out_of_scope for pid, v in sorted(vocabularies.items())
                if v.suppressed_out_of_scope},
            "note": "a term defined only in another part is not matched here; "
                    "no definition governs it outside its own part",
        },
        "typo_density": {
            "threshold": config.TYPO_DENSITY_THRESHOLD,
            "sections_scored": len(signal.sections),
            "sections_typo_dense": len(signal.dense_sections()),
            "dense_sections": [s.section_path for s in signal.dense_sections()],
        },
        "routing": routing.summarise(queues),
        "llm": llm.summary(),
        "audit_sample": {k: v for k, v in sample["sample"].items() if k != "cells"},
        "inflection_gap": matching.inflection_gap(trees, vocabularies, kept),
        "violations": len(violations),
    }
    dump(vocab_dir / "summary.json", summary)

    if violations:
        dump(vocab_dir / "violations.json",
             {"count": len(violations), "violations": violations})

    # Schema round trip: the two files stage 7 and stage 8 read must validate.
    [DefinitionSite.model_validate(json.loads(s.model_dump_json())) for s in schema_sites]
    [TermUse.model_validate(json.loads(u.model_dump_json())) for u in schema_uses]

    if not args.quiet:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\nwrote {vocab_dir}")
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
