"""Stage 8 CLI. `python -m pipeline.eval [--full]` from the repo root.

Writes `output/<run>/eval/report.json` and `report.md` with the ten sections of
handover/SPEC.md 2.6, and `violations.json` beside them. Exit 0 when every gate
in `config.GATES` passes or is honestly skipped, 2 when one fails, 1 on an
internal error.

Scope, per SPEC 2.6: by default the report covers the parts the run has, and
`--full` runs the whole battery, widening the provided-artifact cross checks
from the parts in scope to the whole document. `--batch B1` narrows to the
parts of that batch in `config.BATCHES`.

Inputs may be absent. Tonight most of the pipeline does not exist, so `--input
auto` falls back to `fixtures/` and stamps that on the report; every section
that cannot measure says which file it looked for and reports `no_data` rather
than a zero.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import config
from pipeline.eval import golden as golden_mod
from pipeline.eval import inputs as inputs_mod
from pipeline.eval import provided as provided_mod
from pipeline.eval import gates as gates_mod
from pipeline.eval import report as report_mod
from pipeline.eval.context import Context
from pipeline.eval.rates import Section
from pipeline.eval.sections import (calibration, concepts, definitions, golden_refs,
                                    golden_terms, invariants, outline, page_map,
                                    stratified_audit, transitions)

# (SPEC 2.6 section name, builder). The order is the report's order, and
# report.assemble asserts the names against pipeline/eval/sections/SECTION_NAMES.
BUILDERS = [
    ("invariants", invariants.build),
    ("page_map_vs_provided", page_map.build),
    ("outline_vs_provided", outline.build),
    ("definitions_vs_provided", definitions.build),
    ("golden_refs", golden_refs.build),
    ("golden_terms", golden_terms.build),
    ("stratified_audit", stratified_audit.build),
    ("confidence_calibration", calibration.build),
    ("resolution_transitions", transitions.build),
    ("concepts", concepts.build),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m pipeline.eval",
        description="Stage 8, the evaluation harness (handover/SPEC.md 2.6).")
    p.add_argument("--full", action="store_true",
                   help="the whole battery over everything, the scheduled sweep mode; "
                        "widens the provided-artifact cross checks to the whole document")
    p.add_argument("--batch", metavar="ID",
                   help=f"limit scope to a batch's parts. one of {sorted(config.BATCHES)}")
    p.add_argument("--run", metavar="ID",
                   help="run id under output/. default: the newest run holding stage "
                        "output, else 'dev'")
    p.add_argument("--input", choices=["auto", "output", "fixtures"], default="auto",
                   help="where stage output is read from. auto prefers the run "
                        "directory and falls back to fixtures/")
    p.add_argument("--output-dir", type=Path, default=config.OUTPUT,
                   help="the output root holding run directories")
    p.add_argument("--fixtures-dir", type=Path, default=config.ROOT / "fixtures")
    p.add_argument("--golden-dir", type=Path, default=config.GOLDEN,
                   help="directory of hand labels, see pipeline/eval/GOLDEN_FORMAT.md")
    p.add_argument("--previous-snapshot", type=Path, default=None,
                   help="ref status snapshot to count resolution transitions against")
    p.add_argument("--no-pdf", action="store_true",
                   help="do not open the PDF; the embedded-outline cross check "
                        "reports no_data")
    p.add_argument("--no-llm", action="store_true",
                   help="draw the stratified audit sample but do not call the checker")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def batches_in(inputs: inputs_mod.Inputs) -> list[str]:
    """Batch ids the loaded stage output carries, from the nodes themselves."""
    seen = {n.batch_id for _part, n in inputs.nodes() if n.batch_id}
    seen |= {r.batch_id for r in inputs.all_refs() if r.batch_id}
    return sorted(seen)


def resolve_scope(args: argparse.Namespace, present: list[str],
                  batches: list[str]) -> tuple[str, list[str], str]:
    """SPEC 2.6: by default the report covers the parts touched by the batch.

    With `--batch` that is exact. Without it, the batch is inferred from the
    stage output's own `batch_id`s when they name exactly one, which is the
    normal case after a batch load. When the output holds several batches there
    is no single batch to be scoped to, so the run covers every part present and
    the report's scope block says which reading applied rather than implying a
    batch it did not have.
    """
    if args.full:
        return "full", present, "whole_document"
    batch = args.batch
    inferred = False
    if batch is None and len(batches) == 1:
        batch, inferred = batches[0], True
    if batch is not None:
        if batch not in config.BATCHES:
            if not inferred:
                raise SystemExit(f"unknown batch {batch!r}; "
                                 f"config.BATCHES has {sorted(config.BATCHES)}")
        else:
            part = config.BATCHES[batch]["part"]
            mode = f"batch:{batch}" + (" (inferred from the stage output's batch_id)"
                                       if inferred else "")
            return mode, [p for p in present if p == part], "in_scope_parts"
    return ("present (no single batch in the stage output; every part present)",
            present, "in_scope_parts")


def build_context(args: argparse.Namespace) -> Context:
    output_root: Path = args.output_dir
    run = args.run or inputs_mod.newest_run(output_root) or "dev"
    run_dir = output_root / run

    source = args.input
    if source == "auto":
        source = (inputs_mod.OUTPUT_SOURCE if inputs_mod.has_stage_output(run_dir)
                  else inputs_mod.FIXTURES_SOURCE)
    source_root = run_dir if source == inputs_mod.OUTPUT_SOURCE else args.fixtures_dir

    present = inputs_mod.discover_parts(source_root)
    # Load everything present first, only to read the batch ids the output
    # carries, then narrow to the scope and reload. Two passes over a handful of
    # JSON files is cheaper than guessing the batch from the file names.
    surveyed = inputs_mod.load(source, source_root, run, present, run_dir=run_dir)
    scope_mode, scope_parts, cross = resolve_scope(args, present, batches_in(surveyed))

    loaded = (surveyed if scope_parts == present
              else inputs_mod.load(source, source_root, run, scope_parts,
                                   run_dir=run_dir))
    page_map_artifact = provided_mod.load_page_map()
    outline_artifact = (provided_mod.ProvidedOutline(state="absent",
                                                     error="--no-pdf: the PDF was not opened")
                        if args.no_pdf else provided_mod.load_outline())
    labels = golden_mod.load(args.golden_dir)

    return Context(
        run=run, run_dir=run_dir, eval_dir=run_dir / "eval", full=args.full,
        scope_mode=scope_mode, scope_parts=scope_parts, cross_check_scope=cross,
        inputs=loaded, page_map=page_map_artifact, outline=outline_artifact,
        golden=labels, previous_snapshot=args.previous_snapshot, batch=args.batch,
        options={"no_pdf": args.no_pdf, "no_llm": args.no_llm},
    )


def run_sections(ctx: Context) -> list[Section]:
    """Every section runs. A section that raises becomes an `error` section
    rather than taking the report down with it, because a partial report is
    worth more than a stack trace."""
    out: list[Section] = []
    for name, builder in BUILDERS:
        try:
            section = builder(ctx)
            if section.name != name:
                raise AssertionError(
                    f"{builder.__module__} returned section {section.name!r}, "
                    f"the SPEC 2.6 name is {name!r}")
            out.append(section)
        except Exception as exc:                          # noqa: BLE001
            section = Section(name)
            section.status = "error"
            section.reason = f"{type(exc).__name__}: {exc}"
            section.data = {"traceback": traceback.format_exc().splitlines()[-6:]}
            section.line(f"**This section failed to run:** `{section.reason}`")
            out.append(section)
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ctx = build_context(args)
    sections = run_sections(ctx)

    metrics: dict[str, object] = {}
    reasons: dict[str, str | None] = {}
    for s in sections:
        metrics.update(s.metrics)
        reasons[s.name] = s.reason
    results = gates_mod.evaluate(config.GATES, metrics, reasons)

    json_path, md_path, payload = report_mod.write(ctx, sections, results)
    violations_path = gates_mod.write_violations(
        ctx.eval_dir / "violations.json", ctx.run, results,
        extra={"report": str(json_path)})
    code = gates_mod.exit_code(results)

    if not args.quiet:
        print(f"eval run={ctx.run} source={ctx.inputs.source} "
              f"scope={ctx.scope_mode} parts={','.join(ctx.scope_parts) or 'none'}")
        for s in sections:
            print(f"  {s.name:<24} {s.status}"
                  + (f"  ({s.reason})" if s.reason else ""))
        for g in results:
            print(f"  gate {g.name:<40} {g.status:<16} "
                  f"observed={g.observed or '-'} threshold={g.threshold}")
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")
        print(f"wrote {violations_path}")
        print(f"exit {code}")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:                                     # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
