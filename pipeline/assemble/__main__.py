"""Stage 2 CLI.

    python -m pipeline.assemble
    python -m pipeline.assemble --parts core-terms
    python -m pipeline.assemble --run full

Reads `output/<run>/layout/*.json`, writes `output/<run>/tree/<part>.json` and
`output/<run>/tree/violations.json`. Exit 0 clean, 2 when the invariant report
holds unexplained violations (output is still written, which is the point of
reporting them rather than repairing them), 1 on failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config

from pipeline.assemble.invariants import UNEXPLAINED_PREFIX, check_tree, stale_ledger_entries
from pipeline.assemble.tree import build_part, renumber
from pipeline.parse.model import dump_json
from pipeline.schemas import Node

DEFAULT_RUN = "current"


def serialise(node: Node) -> dict:
    """Tree JSON matching the committed fixtures: no null fields and no empty
    collections, so a node carries only what it actually has. Every dropped key
    re-defaults on load through the same `Node` model."""
    data = node.model_dump(mode="json", exclude_none=True)
    return _prune(data)


def _prune(value):
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if not (isinstance(v, list) and not v)}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def count_kinds(node: Node, out: dict[str, int]) -> None:
    out[node.kind] = out.get(node.kind, 0) + 1
    for child in node.children:
        count_kinds(child, out)


def count_anomalies(node: Node) -> int:
    return len(node.anomalies) + sum(count_anomalies(c) for c in node.children)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.assemble")
    parser.add_argument("--run", default=DEFAULT_RUN, help="run directory under output/")
    parser.add_argument("--parts", default=None, help="comma-separated part ids; default all present")
    parser.add_argument("--profile", default=None, help="hierarchy profile name")
    args = parser.parse_args(argv)

    layout_dir = config.OUTPUT / args.run / "layout"
    if not layout_dir.is_dir():
        print(f"assemble: no layout directory at {layout_dir}", file=sys.stderr)
        return 1
    files = sorted(p for p in layout_dir.glob("*.json") if p.name != "_index.json")
    if args.parts:
        wanted = {p.strip() for p in args.parts.split(",") if p.strip()}
        files = [p for p in files if p.stem in wanted]
        missing = wanted - {p.stem for p in files}
        if missing:
            print(f"assemble: no layout for {', '.join(sorted(missing))}", file=sys.stderr)
            return 1
    if not files:
        print(f"assemble: no layout files in {layout_dir}", file=sys.stderr)
        return 1

    tree_dir = config.OUTPUT / args.run / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)

    reports = []
    total_unexplained = 0
    for path in files:
        layout = json.loads(path.read_text(encoding="utf-8"))
        profile_name = args.profile or layout["profile"]
        profile = config.HIERARCHY_PROFILES[profile_name]
        root, part_anomalies = build_part(layout, profile)
        renumber(root)
        validated = Node.model_validate(root.model_dump())
        out_path = tree_dir / f"{layout['part']['id']}.json"
        out_path.write_text(dump_json(serialise(validated)), encoding="utf-8")

        report = check_tree(layout["part"]["id"], validated)
        _attach_violations(validated, report)
        # Re-write with the geometric anomalies recorded on their nodes.
        out_path.write_text(dump_json(serialise(validated)), encoding="utf-8")
        reports.append(report)
        total_unexplained += len(report.unexplained)

        kinds: dict[str, int] = {}
        count_kinds(validated, kinds)
        print(
            f"assemble: {layout['part']['id']:<38} nodes={sum(kinds.values()):<6} "
            f"violations={len(report.violations):<5} unexplained={len(report.unexplained):<4} "
            f"anomalies={count_anomalies(validated):<5} -> {out_path.relative_to(config.ROOT)}"
        )

    # The manifest lives at the run root, never inside tree/. Stage 8
    # enumerates tree/*.json as part files and tried to validate this one as a
    # Node; tree/ holds part files and nothing else.
    stale = stale_ledger_entries(reports)
    for entry in stale:
        print(
            f"assemble: STALE verified-typesetting entry {entry['part']} {entry['path']} "
            f"(page {entry['page']}, {entry['check']}) matched no violation",
            file=sys.stderr,
        )
    violations_path = config.OUTPUT / args.run / "violations.json"
    legacy = tree_dir / "violations.json"
    if legacy.exists():
        legacy.unlink()
    violations_path.write_text(
        dump_json(
            {
                "run": args.run,
                "total_violations": sum(len(r.violations) for r in reports),
                "total_unexplained": total_unexplained,
                # Ledger entries that explained nothing this run. Surfaced so a
                # human observation that has drifted off its node is visible
                # rather than sitting silently in the code.
                "stale_verified_entries": stale,
                "parts": [r.as_json() for r in reports],
            }
        ),
        encoding="utf-8",
    )
    print(
        f"assemble: {len(files)} part(s), {sum(len(r.violations) for r in reports)} violation(s), "
        f"{total_unexplained} unexplained -> {violations_path.relative_to(config.ROOT)}"
    )
    return 2 if total_unexplained else 0


def _attach_violations(root: Node, report) -> None:
    by_path: dict[str, Node] = {}

    def index(node: Node) -> None:
        by_path[node.path] = node
        for child in node.children:
            index(child)

    index(root)
    for violation in report.violations:
        node = by_path.get(violation.path)
        if node is None:
            continue
        # One anomaly instance per violation, stamped on the violating node,
        # which is one of the two the check compared. Not deduplicated: stage 8
        # matches explanations to violations one for one, so collapsing two
        # identical stamps would leave a violation looking unexplained.
        #
        # An explanation is keyed `<check_id>_<reason>`. An unexplained
        # violation is stamped under `unresolved_<check_id>` instead, because a
        # bare `<check_id>:` key would start with the check id and stage 8 would
        # read the complaint as its own explanation.
        if violation.explained:
            note = f"{violation.explained} ({violation.check}: {violation.detail})"
        else:
            note = f"{UNEXPLAINED_PREFIX}_{violation.check}: {violation.detail}"
        node.anomalies.append(note)


if __name__ == "__main__":
    raise SystemExit(main())
