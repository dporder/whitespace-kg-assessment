"""Stage 3, `python -m pipeline.references`. Trees in, refs out.

    output/<run>/refs/<part>.json            RefsFile, the resolution output
    output/<run>/refs/detection/<part>.json  the detection output, scored apart
    output/<run>/refs/legislation.json       normalised Legislation records
    output/<run>/refs/llm_queue.json         residue awaiting a reachable model
    output/<run>/refs/review_queue.json      what only a human can settle
    output/<run>/refs/report.json            counts, detection and resolution apart
    output/<run>/refs/violations.json        written whenever the exit code is 2

Detection and resolution are separately specified, separately evaluated steps
(SPEC 2.2), so they are separately reported here: the detection counts answer
"did we find the pointing words", the resolution counts answer "did we point
them somewhere", and no single number hides which half is weak.

Scope. `--parts` or `--batch` chooses which parts are *detected* in; resolution
always runs against every tree the run holds, because a citation's target is
usually in another part and the whole point of batched ingestion is that refs
flip from unresolved to resolved as their targets arrive. `--reresolve` re-runs
resolution only, over the refs already written, which is what SPEC 3 asks for
after each batch load.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import config
from pipeline import llm
from pipeline.schemas import Legislation, Node, RefsFile

from . import nearmiss, residue
from .build import Identity, infer_identity, ref_node, span_intact
from .corpus import Corpus
from .detect import PartDetection, Pointer, detect_part
from .resolve import resolve_pointer

REFERENCES_DEFAULTS = {
    "max_range_expansion": 60,      # inclusive members a single range may mint
    "residue_top_candidates": 5,    # SPEC 2.2 pins 5; kept here so it is not buried
}


def settings() -> dict:
    out = dict(REFERENCES_DEFAULTS)
    out.update(getattr(config, "REFERENCES", {}) or {})
    return out


# --------------------------------------------------------------------------
# input discovery, mirroring pipeline/eval so both stages read alike
# --------------------------------------------------------------------------
def has_trees(run_dir: Path) -> bool:
    d = run_dir / "tree"
    return d.is_dir() and any(d.glob("*.json"))


def newest_run(output_root: Path) -> Optional[str]:
    if not output_root.is_dir():
        return None
    runs = [d for d in output_root.iterdir() if d.is_dir() and has_trees(d)]
    if not runs:
        return None
    return sorted(runs, key=lambda d: (d.stat().st_mtime, d.name))[-1].name


# Files that sit beside part files in tree/ and are not parts.
MANIFEST_NAMES = frozenset({"violations.json", "manifest.json", "profile.json",
                            "index.json", "quarantine.json"})


def discover_parts(source_root: Path) -> tuple[list[str], list[dict]]:
    """Part ids in the input source, and the files skipped for not being parts.

    A stage 2 part file is a Node: it has a kind and a path. Anything else in
    `tree/` is a manifest, not a broken part, and saying so is the difference
    between a tidy run and a spurious violation.
    """
    tree_dir = source_root / "tree"
    if not tree_dir.is_dir():
        return [], []
    parts, skipped = [], []
    for path in sorted(tree_dir.glob("*.json")):
        if path.name in MANIFEST_NAMES:
            skipped.append({"path": str(path), "reason": "a known manifest name"})
            continue
        try:
            payload = json.loads(path.read_text())
        except Exception as exc:                          # noqa: BLE001
            parts.append(path.stem)      # a file that will not parse is a broken
            continue                     # part, reported by the loader, not skipped
        if not (isinstance(payload, dict) and "kind" in payload and "path" in payload):
            skipped.append({"path": str(path),
                            "reason": "no kind/path at the top level, so not a part file"})
            continue
        parts.append(path.stem)
    return parts, skipped


def load_part_registry(run_dir: Path, output_root: Path) -> tuple[dict, str]:
    """Parts the document has but this run has not ingested, id to title.

    Used for candidates only. Stage 0's full structural pass derives the part
    map for all 475 pages, so once it lands a citation to `Schedule 6
    (Materials)` can name the part it means even in a batch that does not hold
    it. Read defensively across the shapes that map could take, because it
    belongs to another worker; anything unreadable is simply absent.
    """
    for candidate in (run_dir / "parts.json", output_root / "parts.json",
                      output_root / "profile.json", run_dir / "profile.json"):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text())
        except Exception:                                 # noqa: BLE001
            continue
        registry: dict = {}
        rows = data.get("parts") if isinstance(data, dict) else data
        if isinstance(rows, dict):
            for key, value in rows.items():
                registry[key] = value if isinstance(value, str) else (
                    value.get("title") if isinstance(value, dict) else None)
        elif isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    part_id = row.get("id") or row.get("part") or row.get("part_id")
                    if part_id:
                        registry[str(part_id)] = row.get("title")
        if isinstance(data, dict) and isinstance(data.get("fit_by_part"), dict):
            for key in data["fit_by_part"]:
                registry.setdefault(key, None)
        if registry:
            return registry, str(candidate)
    return {}, ""


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m pipeline.references",
        description="Stage 3, reference detection and resolution (handover/SPEC.md 2.2).")
    p.add_argument("--run", metavar="ID",
                   help="run id under output/. default: the newest run holding trees, "
                        "else 'dev'")
    p.add_argument("--input", choices=["auto", "output", "fixtures"], default="auto",
                   help="where trees are read from. auto prefers the run directory and "
                        "falls back to fixtures/")
    p.add_argument("--parts", metavar="IDS",
                   help="comma-separated part ids to detect in. default: every part present")
    p.add_argument("--batch", metavar="ID",
                   help=f"detect in this batch's part. one of {sorted(config.BATCHES)}")
    p.add_argument("--output-dir", type=Path, default=config.OUTPUT,
                   help="the output root holding run directories")
    p.add_argument("--fixtures-dir", type=Path, default=config.ROOT / "fixtures")
    p.add_argument("--document", help="override the document id used for node ids")
    p.add_argument("--version", help="override the version used for node ids")
    p.add_argument("--no-llm", action="store_true",
                   help="deterministic only: the residue is queued, never sent")
    p.add_argument("--no-cache", action="store_true",
                   help="ignore the llm replay cache and call the model again")
    p.add_argument("--reresolve", action="store_true",
                   help="re-run resolution over refs already written, for the batch "
                        "arrival case, and count the transitions")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------
def load_trees(source_root: Path, parts: list[str]) -> tuple[dict[str, Node], list[dict]]:
    trees, failures = {}, []
    for part in parts:
        path = source_root / "tree" / f"{part}.json"
        try:
            trees[part] = Node.model_validate(json.loads(path.read_text()))
        except Exception as exc:                          # noqa: BLE001
            failures.append({"part": part, "path": str(path),
                             "error": f"{type(exc).__name__}: {exc}"})
    return trees, failures


def resolve_part(part: str, detection: PartDetection, corpus: Corpus,
                 identity: Identity, batch_id: Optional[str]
                 ) -> tuple[list[Node], list[Legislation], dict[str, dict], list[dict]]:
    """One ref node per detected pointer, resolved by the scope rules."""
    refs: list[Node] = []
    statutes: list[Legislation] = []
    contexts: dict[str, dict] = {}
    violations: list[dict] = []
    seen: set[str] = set()
    for order, pointer in enumerate(detection.pointers):
        parent = corpus.node(pointer.parent_path)
        if parent is None:
            violations.append({"kind": "missing_parent", "part": part,
                               "path": pointer.parent_path})
            continue
        resolution = resolve_pointer(corpus, pointer)
        ref = ref_node(pointer, resolution, parent, identity, order=order,
                       batch_id=batch_id)
        if ref.path in seen:
            violations.append({"kind": "duplicate_ref_path", "part": part,
                               "path": ref.path,
                               "detail": "two refs claim the same characters"})
            continue
        seen.add(ref.path)
        if not span_intact(ref, parent):
            violations.append({"kind": "span_mismatch", "part": part, "path": ref.path,
                               "detail": f"text {ref.text!r} is not the characters "
                                         f"{ref.char_span} of its parent"})
        refs.append(ref)
        contexts[ref.path] = {"parent_path": pointer.parent_path, "part": part,
                              "unit_label": parent.unit_label,
                              "sentence": pointer.sentence,
                              "notes": resolution.notes + pointer.notes}
        if pointer.ref_kind == "legislation" and pointer.legislation:
            meta = pointer.legislation
            provision = (f"{meta.get('provision_unit') or 'section'}/{pointer.provision}"
                         if pointer.provision else None)
            statutes.append(Legislation(
                key=ref.target_path or meta["key"], title=meta["title"],
                year=int(meta.get("year") or 0),
                instrument_kind=meta["instrument_kind"], provision=provision))
    return refs, statutes, contexts, violations


def status_counts(refs: list[Node]) -> dict:
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_resolver: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    cross: dict[str, dict[str, int]] = {}
    for ref in refs:
        by_status[ref.status or "?"] = by_status.get(ref.status or "?", 0) + 1
        by_kind[ref.ref_kind or "?"] = by_kind.get(ref.ref_kind or "?", 0) + 1
        by_resolver[ref.resolver or "?"] = by_resolver.get(ref.resolver or "?", 0) + 1
        by_scope[ref.scope_rule or "?"] = by_scope.get(ref.scope_rule or "?", 0) + 1
        cross.setdefault(ref.ref_kind or "?", {})
        cross[ref.ref_kind or "?"][ref.status or "?"] = \
            cross[ref.ref_kind or "?"].get(ref.status or "?", 0) + 1
    return {"total": len(refs),
            "by_status": dict(sorted(by_status.items())),
            "by_ref_kind": dict(sorted(by_kind.items())),
            "by_resolver": dict(sorted(by_resolver.items())),
            "by_scope_rule": dict(sorted(by_scope.items())),
            "by_kind_and_status": {k: dict(sorted(v.items()))
                                   for k, v in sorted(cross.items())},
            "with_candidates": sum(1 for r in refs if r.candidates),
            "with_target": sum(1 for r in refs if r.target_path),
            "queued_for_llm": sum(1 for r in refs
                                  if any(a.startswith(residue.QUEUE_MARKER)
                                         for a in r.anomalies))}


def walk_tree(node: Node):
    yield node
    for child in node.children:
        yield from walk_tree(child)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    output_root: Path = args.output_dir
    run = args.run or newest_run(output_root) or "dev"
    run_dir = output_root / run
    source = args.input
    if source == "auto":
        source = "output" if has_trees(run_dir) else "fixtures"
    source_root = run_dir if source == "output" else args.fixtures_dir

    present, not_parts = discover_parts(source_root)
    if not present:
        print(f"no trees under {source_root / 'tree'}; nothing to do", file=sys.stderr)
        return 1
    scope = present
    if args.batch:
        if args.batch not in config.BATCHES:
            print(f"unknown batch {args.batch!r}; config.BATCHES has "
                  f"{sorted(config.BATCHES)}", file=sys.stderr)
            return 1
        scope = [p for p in present if p == config.BATCHES[args.batch]["part"]]
    if args.parts:
        wanted = [p.strip() for p in args.parts.split(",") if p.strip()]
        scope = [p for p in present if p in wanted]
    if not scope:
        print("no parts in scope after filtering", file=sys.stderr)
        return 1

    refs_dir = run_dir / "refs"
    llm.set_run(run, output_root)
    llm.set_cache_enabled(not args.no_cache)

    trees, failures = load_trees(source_root, present)
    violations: list[dict] = [{"kind": "tree_load_failed", **f} for f in failures]
    corpus = Corpus.from_trees(trees)
    registry, registry_source = load_part_registry(run_dir, output_root)
    if registry:
        corpus.register_parts(registry)
    identity = infer_identity(trees.values(), document=args.document,
                              version=args.version)
    if not identity.verified:
        violations.append({"kind": "identity_unverified", "detail": identity.note,
                           "consequence": "ref ids would not join their parents in the "
                                          "graph; refs were still written for inspection"})

    cfg = settings()
    batch_of = {v["part"]: k for k, v in config.BATCHES.items()}
    all_refs: dict[str, list[Node]] = {}
    contexts: dict[str, dict] = {}
    statutes: list[Legislation] = []
    detections: dict[str, PartDetection] = {}
    span_reports: dict[str, dict] = {}
    previous: dict[str, str] = {}

    for part in scope:
        root = trees.get(part)
        if root is None:
            continue
        if args.reresolve:
            existing = refs_dir / f"{part}.json"
            if existing.exists():
                try:
                    previous.update({r.path: r.status or "?" for r in
                                     RefsFile.model_validate(
                                         json.loads(existing.read_text())).refs})
                except Exception as exc:                  # noqa: BLE001
                    violations.append({"kind": "previous_refs_unreadable", "part": part,
                                       "error": f"{type(exc).__name__}: {exc}"})
        detection = detect_part(part, root, max_range=int(cfg["max_range_expansion"]))
        # Rung three of the ladder: a model sees the orphan sentences and
        # nothing else, and only spans that reproduce their own characters are
        # kept (SPEC 2.2).
        node_text = {n.path: (n.text or "") for n in walk_tree(root)}
        extracted, span_report = residue.extract_spans(
            detection.llm_sentences, node_text, no_llm=args.no_llm)
        span_reports[part] = span_report
        # The model is shown a whole sentence, so it can quote a citation the
        # grammar already found sitting elsewhere in that sentence. Those are
        # dropped rather than minted: two refs on one span would collide on one
        # path and one id, and the graph would silently keep whichever landed
        # second.
        taken = {p.parent_path: [] for p in detection.pointers}
        for p in detection.pointers:
            taken[p.parent_path].append(p.span)
        for item in extracted:
            start, end = item["span"]
            if any(start < b and a < end
                   for a, b in taken.get(item["node_path"], ())):
                span_report.setdefault("overlapped_existing", []).append(
                    {"node_path": item["node_path"], "text": item["text"],
                     "reason": "the grammar had already detected these characters"})
                continue
            taken.setdefault(item["node_path"], []).append((start, end))
            detection.pointers.append(Pointer(
                parent_path=item["node_path"], part=part, span=item["span"],
                text=item["text"], ref_kind=item["ref_kind"], unit=item["ref_kind"],
                method="llm", sentence=item["sentence"],
                notes=["llm_span_extraction: the grammar and the orphan scan did not "
                       "cover these characters"]))
        detections[part] = detection
        refs, part_statutes, part_contexts, part_violations = resolve_part(
            part, detection, corpus, identity, batch_of.get(part) or root.batch_id)
        all_refs[part] = refs
        statutes.extend(part_statutes)
        contexts.update(part_contexts)
        violations.extend(part_violations)

    flat = [r for part in scope for r in all_refs.get(part, [])]
    residue_report = residue.run(flat, contexts, corpus, no_llm=args.no_llm)
    near_miss = nearmiss.route(statutes, no_llm=args.no_llm)

    # -- write ---------------------------------------------------------------
    for part in scope:
        refs = all_refs.get(part, [])
        try:
            payload = RefsFile(part=part, refs=refs)
        except Exception as exc:                          # noqa: BLE001
            violations.append({"kind": "refs_file_invalid", "part": part,
                               "error": f"{type(exc).__name__}: {exc}"})
            continue
        write_json(refs_dir / f"{part}.json",
                   payload.model_dump(mode="json", exclude_none=True,
                                      exclude_defaults=True))
        write_json(refs_dir / "detection" / f"{part}.json", detections[part].as_dict())

    unique_statutes = {s.key: s for s in statutes}
    write_json(refs_dir / "legislation.json",
               {"count": len(unique_statutes),
                "by_instrument_kind": _counter(s.instrument_kind
                                               for s in unique_statutes.values()),
                "with_provision": sum(1 for s in unique_statutes.values() if s.provision),
                "near_miss_routing": near_miss,
                "records": [s.model_dump() for s in
                            sorted(unique_statutes.values(), key=lambda s: s.key)]})
    write_json(refs_dir / "llm_queue.json", residue.queue_file(residue_report))

    review = [{"ref_path": r.path, "part": r.path.split("/", 1)[0], "status": r.status,
               "ref_kind": r.ref_kind, "text": r.text,
               "candidates": [c.model_dump() for c in r.candidates],
               "why": [a for a in r.anomalies]}
              for r in flat if r.status in ("ambiguous", "unresolved")]
    write_json(refs_dir / "review_queue.json",
               {"count": len(review),
                "note": "ambiguous and unresolved refs, the rung after the model; the "
                        "review UI reads the refs files themselves, this is the digest",
                "items": review})

    detection_counts = {part: detections[part].counts() for part in sorted(detections)}
    report = {
        "stage": 3,
        "run": run,
        "input": {"source": source, "root": str(source_root),
                  "parts_present": present, "parts_in_scope": scope,
                  "skipped_not_parts": not_parts,
                  "part_registry": {"source": registry_source or None,
                                    "parts_known_but_not_ingested":
                                        sorted(set(registry) - set(trees))}},
        "identity": identity.as_dict(),
        "settings": cfg,
        "config_keys_requested": [
            "REFERENCES.max_range_expansion", "REFERENCES.residue_top_candidates",
            *near_miss["config_keys_requested"],
            "LLM.max_attempts", "LLM.backoff_base_seconds", "LLM.backoff_max_seconds",
            "LLM.max_tokens", "LLM.timeout_seconds",
        ],
        "detection": {
            "per_part": detection_counts,
            "totals": _sum_counts(detection_counts.values()),
            "note": "detection is scored separately from resolution (SPEC 2.2); these "
                    "counts answer whether the pointing words were found at all",
        },
        "resolution": {
            "per_part": {part: status_counts(all_refs.get(part, [])) for part in scope},
            "totals": status_counts(flat),
            "note": "confidence is left unset on everything the grammar and the scope "
                    "rules settled: a deterministic resolver carries the measured "
                    "precision of its class from stage 8, never a number it made up",
        },
        "residue": {k: v for k, v in residue_report.items() if k != "queue"},
        "span_extraction": {"note": "rung three of the fallback ladder: a model sees "
                                    "orphan sentences only, and a span it returns is "
                                    "kept only if its characters are really there",
                            "per_part": span_reports},
        "legislation": {"distinct_keys": len(unique_statutes),
                        "mentions": len(statutes),
                        "near_miss": {k: v for k, v in near_miss.items() if k != "pairs"}},
        "review_queue": len(review),
        "ref_boxes": ("copied from the citing node's own boxes: stage 3 reads trees, "
                      "not layout, so the tight box for the citing characters cannot "
                      "be computed here. The box does contain the citation."),
        "transitions": _transitions(previous, flat) if args.reresolve else None,
        "violations": violations,
    }
    write_json(refs_dir / "report.json", report)
    if violations:
        write_json(refs_dir / "violations.json",
                   {"count": len(violations), "violations": violations})

    if not args.quiet:
        _print_summary(report)
    return 2 if violations else 0


def _counter(values) -> dict:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def _sum_counts(counts) -> dict:
    total: dict = {}
    for c in counts:
        for key, value in c.items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
            else:
                bucket = total.setdefault(key, {})
                for k, v in value.items():
                    bucket[k] = bucket.get(k, 0) + v
    return total


def _transitions(previous: dict[str, str], refs: list[Node]) -> dict:
    moved = [{"ref_path": r.path, "from": previous[r.path], "to": r.status}
             for r in refs if r.path in previous and previous[r.path] != r.status]
    return {"compared": len(previous), "changed": len(moved),
            "unresolved_to_resolved": sum(1 for m in moved
                                          if m["from"] == "unresolved"
                                          and m["to"] == "resolved"),
            "changes": moved[:50]}


def _print_summary(report: dict) -> None:
    det, res = report["detection"]["totals"], report["resolution"]["totals"]
    print(f"stage 3  run={report['run']}  source={report['input']['source']}  "
          f"parts={','.join(report['input']['parts_in_scope'])}")
    versions = report["identity"]["versions"]
    shown = (report["identity"]["one_version_for_every_part"]
             or ", ".join(f"{p}={v}" for p, v in sorted(versions.items())))
    print(f"  identity   document={report['identity']['document']} "
          f"version(s)={shown} verified={report['identity']['verified']}")
    print(f"  DETECTION  pointers={det.get('pointers', 0)}  "
          f"by kind {det.get('by_ref_kind', {})}")
    print(f"             by method {det.get('by_method', {})}  "
          f"anaphora={det.get('anaphora', 0)}  "
          f"range-expanded={det.get('range_expanded', 0)}")
    print(f"             orphan keywords={det.get('orphan_keywords', 0)} "
          f"{det.get('orphans_by_verdict', {})}  "
          f"title citations={det.get('title_citations', 0)}")
    print(f"  RESOLUTION refs={res['total']}  by status {res['by_status']}")
    print(f"             by kind   {res['by_ref_kind']}")
    print(f"             by resolver {res['by_resolver']}  "
          f"scope rules {res['by_scope_rule']}")
    print(f"  RESIDUE    considered={report['residue']['considered']} "
          f"called={report['residue']['called']} "
          f"resolved={report['residue']['resolved']} "
          f"none={report['residue']['answered_none']} "
          f"chose-uningested={report['residue']['chose_uningested_target']} "
          f"queued={report['residue']['queued']}")
    spend = report["residue"].get("spend") or {}
    if spend:
        print(f"             spend: api calls={spend.get('api_calls', 0)} "
              f"cache hits={spend.get('cache_hits', 0)} "
              f"tokens in/out={spend.get('input_tokens', 0)}/"
              f"{spend.get('output_tokens', 0)}")
    if report["residue"].get("reason"):
        print(f"             reason: {report['residue']['reason']}")
    print(f"  LEGISLATION distinct={report['legislation']['distinct_keys']} "
          f"mentions={report['legislation']['mentions']} "
          f"near-miss pairs={report['legislation']['near_miss']['pairs_over_threshold']}")
    print(f"  REVIEW     queue={report['review_queue']}")
    if report["violations"]:
        print(f"  VIOLATIONS {len(report['violations'])}: "
              f"{_counter(v['kind'] for v in report['violations'])}")


if __name__ == "__main__":
    raise SystemExit(main())
