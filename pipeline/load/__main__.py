"""Stage 7, `python -m pipeline.load --batch <id>`. The join.

    output/<run>/graph/nodes.jsonl        one row per graph node
    output/<run>/graph/edges.jsonl        GraphEdge rows, SPEC 2.5
    output/<run>/graph/graph.json         the NetworkX export, same producer
    output/<run>/graph/audit.jsonl        merges, sweeps, rollbacks, dedups
    output/<run>/graph/load_report.json   counts, reconciled against the inputs

Reads every stage output through `pipeline.eval.inputs`, which already answers
the three questions that matter separately: was the file there, did it parse,
did it validate. Stage 7 is the one place that sees stages 2 to 6 at once, so it
is where `ASSOCIATED_TERM` and `DEFINED_USING` are computed (SPEC 2.4: the
aggregation joins stage 4 and stage 5 outputs, which must not read each other).

The loader reports its own node and edge counts against the stage outputs it
read, because a count that does not reconcile is the failure EVALUATION 4 makes
a release gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import config
from pipeline.eval import inputs as inputs_mod
from pipeline.schemas import Legislation, Node, node_id, lineage_key

from . import associated, export, salience as salience_mod
from .audit import Audit
from .neo4j_loader import Graph, duplicate_edge_keys
from .rows import (Rows, concept_rows, dangling_endpoints, dedupe,
                   legislation_rows, term_rows, tree_rows, walk)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m pipeline.load",
        description="Stage 7, the graph load (handover/SPEC.md 2.5).")
    p.add_argument("--batch", metavar="ID",
                   help=f"the batch being loaded, one of {sorted(config.BATCHES)}, or "
                        f"any id for a throwaway load. default: inferred from the "
                        f"stage output's own batch ids")
    p.add_argument("--run", metavar="ID",
                   help="run id under output/. default: the newest run holding stage "
                        "output, else 'dev'")
    p.add_argument("--input", choices=["auto", "output", "fixtures"], default="auto")
    p.add_argument("--parts", metavar="IDS", help="comma-separated part ids to load")
    p.add_argument("--output-dir", type=Path, default=config.OUTPUT)
    p.add_argument("--fixtures-dir", type=Path, default=config.ROOT / "fixtures")
    p.add_argument("--no-neo4j", action="store_true",
                   help="write the JSONL and the NetworkX export, load nothing")
    p.add_argument("--no-sweep", action="store_true",
                   help="skip the sweep; the load then only avoids duplicates rather "
                        "than converging on state")
    p.add_argument("--no-salience", action="store_true")
    p.add_argument("--rollback", metavar="BATCH",
                   help="remove a batch completely and exit")
    p.add_argument("--sweep-only", action="store_true",
                   help="run the sweep for --batch over --parts and exit")
    p.add_argument("--document-path", default=None,
                   help="path for the document root node when the trees carry none")
    p.add_argument("--access-label", default=None,
                   help="access classification inherited by every node (DESIGN 4)")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def write_jsonl(path: Path, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return len(rows)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def document_root(trees: dict[str, Node], path: Optional[str],
                  batch_id: str) -> tuple[Optional[Node], Optional[dict]]:
    """The graph's root. Synthesised only when no tree carries one.

    SPEC 2.5 lists `CONTAINS` from document to part, but stage 2 writes one file
    per part and no document node, so unless a part tree carries a `document`
    root there is nothing for those edges to start from. Rather than drop the
    edges or invent a root silently, one is synthesised, flagged in its own
    `anomalies`, and reported.
    """
    for root in trees.values():
        if root.kind == "document":
            return root, None
    if not trees:
        return None, None
    sample = next(iter(trees.values()))
    document = _infer_document_id(sample)
    root_path = path or document
    note = {"kind": "document_root_synthesised",
            "path": root_path,
            "detail": "no tree carried a node of kind 'document', so stage 7 minted the "
                      "root that CONTAINS the parts. Stage 2 writes one file per part; "
                      "if it later emits a document root, this stops happening."}
    node = Node(id=node_id(document, _infer_version(sample, document), root_path),
                lineage_key=lineage_key(document, root_path),
                path=root_path, kind="document", citable=False,
                page_start=min(r.page_start for r in trees.values()),
                page_end=max(r.page_end for r in trees.values()),
                order=0, batch_id=batch_id,
                anomalies=[f"document_root_synthesised_by_stage_7: {note['detail']}"])
    return node, note


def _infer_document_id(sample: Node) -> str:
    for candidate in (config.DOCUMENT_ID, f"{config.DOCUMENT_ID}-fixture"):
        if lineage_key(candidate, sample.path) == sample.lineage_key:
            return candidate
    return config.DOCUMENT_ID


def _infer_version(sample: Node, document: str) -> str:
    for candidate in ("v1", "v3.0.11", "dev", sample.template_version or "",
                      sample.version_label or ""):
        if candidate and node_id(document, candidate, sample.path) == sample.id:
            return candidate
    return "v1"


def load_legislation_records(source_root: Path) -> list[Legislation]:
    path = source_root / "refs" / "legislation.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:                                     # noqa: BLE001
        return []
    out = []
    for row in data.get("records", []):
        try:
            out.append(Legislation.model_validate(row))
        except Exception:                                 # noqa: BLE001
            continue
    return out


def build_rows(inputs, source_root: Path, batch_id: str, *,
               document_path: Optional[str], access_label: Optional[str]
               ) -> tuple[Rows, list[dict], Optional[Node]]:
    document, note = document_root(inputs.trees, document_path, batch_id)
    notes = [note] if note else []
    rows = tree_rows(inputs.trees, inputs.refs, batch_id=batch_id, document=document,
                     access_label=access_label)
    nodes_by_id = {n.id: n for part in inputs.trees for n in walk(inputs.trees[part])}

    leg = legislation_rows(inputs.refs, load_legislation_records(source_root),
                           batch_id=batch_id)
    rows.nodes.extend(leg.nodes)
    rows.edges.extend(leg.edges)
    rows.notes.extend(leg.notes)

    if inputs.definition_sites is not None or inputs.term_uses is not None:
        terms = term_rows(inputs.definition_sites or [], inputs.term_uses or [],
                          nodes_by_id, batch_id=batch_id)
        rows.nodes.extend(terms.nodes)
        rows.edges.extend(terms.edges)
        rows.notes.extend(terms.notes)

    if inputs.concepts:
        concepts = concept_rows(inputs.concepts, nodes_by_id, batch_id=batch_id)
        rows.nodes.extend(concepts.nodes)
        rows.edges.extend(concepts.edges)
        rows.notes.extend(concepts.notes)
        joined = associated.build(inputs.concepts, inputs.term_uses or [], nodes_by_id,
                                  batch_id=batch_id)
        rows.edges.extend(joined.edges)
        rows.notes.extend(joined.notes)

    rows.notes.extend(notes)
    return rows, notes, document


def reconcile(inputs, rows: Rows, document: Optional[Node]) -> dict:
    """The loader's own counts against the stage outputs it read."""
    tree_nodes = sum(1 for part in inputs.trees for _ in walk(inputs.trees[part]))
    ref_nodes = sum(len(v) for v in inputs.refs.values())
    expected_nodes = tree_nodes + ref_nodes + (1 if document is not None else 0)
    counted = rows.counts()
    referents = sum(counted["nodes_by_label"].get(label, 0)
                    for label in ("Term", "Legislation", "Concept"))
    return {
        "stage_inputs": {
            "tree_nodes": tree_nodes, "refs": ref_nodes,
            "definition_sites": len(inputs.definition_sites or []),
            "term_uses": len(inputs.term_uses or []),
            "concepts": len(inputs.concepts or []),
            "document_root": 1 if document is not None else 0,
        },
        "graph_rows": counted,
        "tree_and_ref_nodes_expected": expected_nodes,
        "tree_and_ref_nodes_written": counted["nodes"] - referents,
        "referent_nodes_written": referents,
        "reconciles": counted["nodes"] - referents == expected_nodes,
    }


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    output_root: Path = args.output_dir
    run = args.run or inputs_mod.newest_run(output_root) or "dev"
    run_dir = output_root / run
    graph_dir = run_dir / "graph"

    source = args.input
    if source == "auto":
        source = (inputs_mod.OUTPUT_SOURCE if inputs_mod.has_stage_output(run_dir)
                  else inputs_mod.FIXTURES_SOURCE)
    source_root = run_dir if source == inputs_mod.OUTPUT_SOURCE else args.fixtures_dir

    # `discover_parts` returns the part files and the non-part files it skipped
    # (a manifest in tree/ is not a broken part). Both are reported.
    present, not_parts = inputs_mod.discover_parts(source_root)
    parts = present
    if args.batch in config.BATCHES:
        parts = [p for p in present if p == config.BATCHES[args.batch]["part"]]
    if args.parts:
        wanted = {p.strip() for p in args.parts.split(",") if p.strip()}
        parts = [p for p in present if p in wanted]

    inputs = inputs_mod.load(source, source_root, run, parts, run_dir=run_dir,
                             skipped=not_parts)
    batch_id = args.batch or _infer_batch(inputs) or "B0"
    audit = Audit(graph_dir / "audit.jsonl", run=run, batch_id=batch_id)

    # -- the two operations that do nothing else ----------------------------
    if args.rollback:
        graph = Graph()
        result = graph.rollback(args.rollback)
        audit.record("rollback", reason="operator asked for a batch to be removed",
                     counts=result, target_batch=args.rollback)
        audit.flush()
        graph.close()
        print(json.dumps(result, indent=2))
        return 0
    if args.sweep_only:
        graph = Graph()
        result = graph.sweep(parts or present, batch_id)
        audit.record("sweep", reason="operator asked for a sweep",
                     affected=result["affected_ids"], counts=result, scope=parts)
        audit.flush()
        graph.close()
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("affected", "affected_ids")}, indent=2))
        return 0

    if not inputs.trees:
        print(f"no trees under {source_root / 'tree'}; nothing to load", file=sys.stderr)
        return 1

    rows, doc_notes, document = build_rows(
        inputs, source_root, batch_id, document_path=args.document_path,
        access_label=args.access_label)
    duplicates = duplicate_edge_keys(rows.edges)
    rows, collapsed = dedupe(rows)
    if collapsed:
        audit.record("dedup", reason="rows sharing one MERGE key were collapsed before "
                                     "the load, so a rerun cannot grow a twin",
                     affected=[c.get("key") or f"{c['type']}:{c['src']}->{c['dst']}"
                               for c in collapsed],
                     counts={"collapsed": len(collapsed),
                             "duplicate_edge_keys": len(duplicates)})

    dangling = dangling_endpoints(rows)
    # Every load is an event in the graph's history, including one that only
    # wrote files: an audit log that starts at the database cannot explain where
    # a row came from.
    audit.record("build", reason=f"rows built for batch {batch_id} from {source}",
                 counts={**rows.counts(), "parts": len(parts),
                         "dangling_edge_endpoints": len(dangling)},
                 parts=parts, source=str(source_root))
    node_rows = [r.as_dict() for r in rows.nodes]
    edge_rows = [e.model_dump() for e in rows.edges]
    write_jsonl(graph_dir / "nodes.jsonl", node_rows)
    write_jsonl(graph_dir / "edges.jsonl", edge_rows)
    exported = export.write(rows, graph_dir / "graph.json",
                            {"run": run, "batch_id": batch_id, "parts": parts,
                             "source": source,
                             "note": "the rows of this load, not the whole graph"})

    tree_nodes = [n for part in inputs.trees for n in walk(inputs.trees[part])]
    scores = salience_mod.compute(tree_nodes, inputs.all_refs(), inputs.term_uses or [])

    report = {
        "stage": 7,
        "run": run,
        "batch_id": batch_id,
        "input": {"source": source, "root": str(source_root), "parts": parts,
                  "failures": [r.as_dict() for r in inputs.failures()],
                  "absent": [r.as_dict() for r in inputs.absences()],
                  "skipped_not_parts": [r.as_dict() for r in inputs.skipped()]},
        "reconciliation": reconcile(inputs, rows, document),
        "associated_term": associated.summary(rows.edges),
        "salience": scores.report,
        "export": exported,
        "notes": rows.notes,
        "dedup": {"collapsed_rows": len(collapsed), "detail": collapsed[:50]},
        "dangling_edge_endpoints": {"count": len(dangling),
                                    "detail": dangling[:50],
                                    "note": "an edge endpoint with no node row: the "
                                            "JSON export would invent the node and the "
                                            "Neo4j load would drop the edge"},
        "config_keys_requested": ["SALIENCE.furniture_min_repeats",
                                  "SALIENCE.furniture_min_parts",
                                  "SALIENCE.outlier_sigma"],
        "neo4j": None,
    }

    if not args.no_neo4j:
        graph = Graph()
        try:
            schema = graph.ensure_schema()
            merged_nodes = graph.merge_nodes(rows.nodes)
            merged_edges = graph.merge_edges(rows.edges)
            audit.record("merge", reason=f"batch {batch_id} loaded, MERGE only",
                         counts={"nodes": merged_nodes, "edges": merged_edges,
                                 **schema})
            swept = None
            if not args.no_sweep:
                swept = graph.sweep(parts, batch_id)
                audit.record("sweep",
                             reason="anything in this batch's scope still carrying an "
                                    "earlier batch tag was not re-asserted by this run",
                             affected=swept["affected_ids"], counts=swept, scope=parts)
            applied = None
            if not args.no_salience:
                applied = graph.apply_salience(scores.values, scores.term_values,
                                               scores.flagged)
                audit.record("salience",
                             reason="recomputed from the graph: breadth * log(1 + "
                                    "frequency), repeated furniture excluded",
                             counts=applied)
            report["neo4j"] = {"schema": schema, "nodes_merged": merged_nodes,
                               "edges_merged": merged_edges, "sweep": swept,
                               "salience": applied, "counts": graph.counts(batch_id)}
        except Exception as exc:                          # noqa: BLE001
            report["neo4j"] = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            graph.close()

    audit.flush()
    write_json(graph_dir / "load_report.json", report)
    if not args.quiet:
        _print_summary(report)
    failed = (report["neo4j"] or {}).get("error") if report["neo4j"] else None
    if failed:
        return 1
    return 0 if report["reconciliation"]["reconciles"] else 2


def _infer_batch(inputs) -> Optional[str]:
    seen = {n.batch_id for _p, n in inputs.nodes() if n.batch_id}
    seen |= {r.batch_id for r in inputs.all_refs() if r.batch_id}
    return sorted(seen)[0] if len(seen) == 1 else None


def _print_summary(report: dict) -> None:
    rec = report["reconciliation"]
    print(f"stage 7  run={report['run']}  batch={report['batch_id']}  "
          f"source={report['input']['source']}  parts={','.join(report['input']['parts'])}")
    print(f"  ROWS       nodes={rec['graph_rows']['nodes']} "
          f"edges={rec['graph_rows']['edges']}")
    print(f"             by label {rec['graph_rows']['nodes_by_label']}")
    print(f"             by type  {rec['graph_rows']['edges_by_type']}")
    print(f"  RECONCILE  stage inputs {rec['stage_inputs']}")
    print(f"             tree+ref nodes expected={rec['tree_and_ref_nodes_expected']} "
          f"written={rec['tree_and_ref_nodes_written']} "
          f"referents={rec['referent_nodes_written']}  "
          f"reconciles={rec['reconciles']}")
    a = report["associated_term"]
    print(f"  ASSOC_TERM edges={a['edges']} min_share={a['min_share']} "
          f"concepts={a['concepts_with_terms']}")
    s = report["salience"]
    print(f"  SALIENCE   scored={s['nodes_scored']} nonzero={s['nodes_with_salience']} "
          f"terms={s['terms_scored']} furniture excluded={s['furniture_nodes_excluded']} "
          f"flagged={s['flagged_out_of_distribution']}")
    print(f"  EXPORT     {report['export']['nodes']} nodes, "
          f"{report['export']['edges']} edges -> {report['export']['path']}")
    neo = report["neo4j"]
    if neo is None:
        print("  NEO4J      skipped (--no-neo4j)")
    elif neo.get("error"):
        print(f"  NEO4J      FAILED: {neo['error']}")
    else:
        print(f"  NEO4J      merged nodes={neo['nodes_merged']} edges={neo['edges_merged']}")
        if neo.get("sweep"):
            print(f"             sweep deleted nodes={neo['sweep']['nodes_deleted']} "
                  f"rels={neo['sweep']['relationships_deleted']} "
                  f"orphan referents={neo['sweep']['orphan_referents_deleted']}")
        if neo.get("salience"):
            print(f"             salience written to nodes="
                  f"{neo['salience']['nodes_updated']} terms="
                  f"{neo['salience']['terms_updated']}")
        print(f"             graph now nodes={neo['counts']['nodes_total']} "
              f"rels={neo['counts']['relationships_total']}")
    if report["dangling_edge_endpoints"]["count"]:
        print(f"  DANGLING   {report['dangling_edge_endpoints']['count']} edge endpoint(s) "
              f"have no node row")
    if report["notes"]:
        kinds: dict[str, int] = {}
        for note in report["notes"]:
            kinds[note["kind"]] = kinds.get(note["kind"], 0) + 1
        print(f"  NOTES      {kinds}")


if __name__ == "__main__":
    raise SystemExit(main())
