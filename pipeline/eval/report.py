"""Report assembly and rendering. Section names are asserted against SPEC 2.6.

Two deliberate choices about the report itself:

**No wall-clock timestamp anywhere in it.** EVALUATION.md section 4 says
"regression is a diff between reports, not a feeling". A timestamp would make
every diff non-empty and destroy that property, so two cold runs over identical
inputs produce a byte-identical report. What replaces it is
`inputs_fingerprint`, a hash over the exact files this run read, which
distinguishes "same inputs, different answer" (a real regression) from
"different inputs" without pretending to be a clock.

`resolution_transitions` is the one deliberate exception, and the header says
so. It reads the ref-status snapshot the previous run wrote, so a second run in
the same output directory reports history the first one did not have. That is
the section doing its job, not the report being non-deterministic: given the
same inputs *and* the same prior snapshot, it too is byte-identical.

**Markdown headings are the bare section names.** `## invariants`, not a prettier
paraphrase, so the SPEC 2.6 contract is greppable in both outputs. The
explanatory sentence goes underneath.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import config
from pipeline.eval.context import Context
from pipeline.eval.gates import GateResult, exit_code
from pipeline.eval.rates import Section
from pipeline.eval.sections import SECTION_NAMES

REPORT_VERSION = "1"

SECTION_SUBTITLES = {
    "invariants": "structural and geometric checks, pass or fail with locations",
    "page_map_vs_provided": "the derived page map against the one in the assignment notes",
    "outline_vs_provided": "the derived tree against the PDF's embedded outline, triaged",
    "definitions_vs_provided": "the discovered vocabulary against the declared schedule",
    "golden_refs": "detection and resolution scored separately, abstention scored",
    "golden_terms": "term uses, per ambiguity kind, cost weighted",
    "stratified_audit": "an independent check on a stratified sample of confident cases",
    "confidence_calibration": "raw score bucket against observed precision, per resolver",
    "resolution_transitions": "per batch, unresolved to resolved",
    "concepts": "duplicate rate, coverage, spot check",
}


def _digest(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except Exception:                                     # noqa: BLE001
        return "unreadable"


def inputs_fingerprint(ctx: Context) -> dict[str, Any]:
    """A hash over everything that can change this report. Replaces a timestamp.

    "Everything" has to mean it, or the fingerprint quietly licenses a false
    conclusion: two runs whose fingerprints match are supposed to be comparable,
    so a changed page map, a re-exported PDF, an edited threshold or a file that
    started failing to load must all move it. Stage output, golden labels, both
    provided artifacts, `config.GATES` and the transitions snapshot are all in.
    A file that failed to load contributes its path and error, because "absent"
    and "present but broken" are different runs.
    """
    entries: list[dict[str, Any]] = []
    for rec in ctx.inputs.records:
        if rec.path is None:
            continue
        if rec.state == "loaded":
            entries.append({"file": str(rec.path), "sha1": _digest(Path(rec.path))})
        elif rec.state == "failed":
            entries.append({"file": str(rec.path), "sha1": "failed-to-load",
                            "error": rec.error})
    for path in ctx.golden.files:
        entries.append({"file": path, "sha1": _digest(Path(path))})

    # The two provided artifacts three sections diff against.
    if ctx.page_map.source_file:
        entries.append({"file": ctx.page_map.source_file,
                        "sha1": _digest(Path(ctx.page_map.source_file)),
                        "role": "provided page map"})
    if ctx.outline.state == "loaded" and ctx.outline.source_file:
        entries.append({"file": ctx.outline.source_file,
                        "sha1": _digest(Path(ctx.outline.source_file)),
                        "role": "embedded outline (PDF)"})

    # The snapshot resolution_transitions compared against, when there was one.
    # That section records the path it actually read; see transitions.build.
    previous = ctx.options.get("previous_snapshot_read")
    if previous:
        entries.append({"file": str(previous), "sha1": _digest(Path(previous)),
                        "role": "ref status snapshot compared against"})

    entries.sort(key=lambda e: (e["file"], e.get("role", "")))
    thresholds = json.dumps(config.GATES, sort_keys=True)
    material = "".join(f"{e['file']}:{e['sha1']}:{e.get('error', '')}" for e in entries)
    combined = hashlib.sha1((material + "|gates:" + thresholds).encode()).hexdigest()
    return {"combined": combined, "files": entries,
            "config_gates": json.loads(thresholds)}


def assemble(ctx: Context, sections: list[Section],
             gates: list[GateResult]) -> dict[str, Any]:
    names = [s.name for s in sections]
    if names != SECTION_NAMES:
        raise AssertionError(
            f"report sections do not match handover/SPEC.md 2.6.\n"
            f"expected: {SECTION_NAMES}\ngot:      {names}")
    return {
        "report_version": REPORT_VERSION,
        "run": ctx.run,
        "scope": {
            "mode": ctx.scope_mode,
            "batch": ctx.batch,
            "full": ctx.full,
            "parts": ctx.scope_parts,
            "cross_checks": ctx.cross_check_scope,
        },
        "input_source": {
            "source": ctx.inputs.source,
            "root": str(ctx.inputs.root),
            "loaded": [r.as_dict() for r in ctx.inputs.records if r.state == "loaded"],
            "absent": [r.as_dict() for r in ctx.inputs.absences()],
            "failed": [r.as_dict() for r in ctx.inputs.failures()],
            "skipped_not_a_part": [r.as_dict() for r in ctx.inputs.skipped()],
        },
        "provided_artifacts": {
            "page_map": {"state": ctx.page_map.state, "source_file": ctx.page_map.source_file,
                         "rows": len(ctx.page_map.rows), "error": ctx.page_map.error},
            "embedded_outline": {"state": ctx.outline.state,
                                 "source_file": ctx.outline.source_file,
                                 "entries": len(ctx.outline.entries),
                                 "error": ctx.outline.error},
        },
        "golden": ctx.golden.as_dict(),
        "inputs_fingerprint": inputs_fingerprint(ctx),
        "gates": {
            "thresholds_from": "config.GATES",
            "exit_code": exit_code(gates),
            "results": [g.as_dict() for g in gates],
        },
        "sections": {s.name: s.as_dict() for s in sections},
    }


def render_markdown(ctx: Context, sections: list[Section],
                    gates: list[GateResult], payload: dict[str, Any]) -> str:
    code = payload["gates"]["exit_code"]
    lines: list[str] = []
    lines.append(f"# Evaluation report, run `{ctx.run}`")
    lines.append("")
    lines.append(f"Stage 8, `python -m pipeline.eval`. Exit code **{code}** "
                 f"({'gates passed' if code == 0 else 'a gate failed, see below'}).")
    lines.append("")
    lines.append(f"- **input source**: `{ctx.inputs.source}` at `{ctx.inputs.root}`")
    lines.append(f"- **scope**: {ctx.scope_mode}, parts "
                 f"{', '.join(ctx.scope_parts) or 'none'}; cross checks over "
                 f"{ctx.cross_check_scope}")
    lines.append(f"- **provided page map**: {ctx.page_map.state}"
                 + (f", from `{ctx.page_map.source_file}`" if ctx.page_map.source_file else ""))
    lines.append(f"- **embedded outline**: {ctx.outline.state}, "
                 f"{len(ctx.outline.entries)} entries"
                 + (f", from `{ctx.outline.source_file}`" if ctx.outline.source_file else ""))
    lines.append(f"- **golden labels**: {len(ctx.golden.records)} in "
                 f"`{ctx.golden.directory}` ({ctx.golden.state})")
    lines.append(f"- **inputs fingerprint**: `{payload['inputs_fingerprint']['combined']}` "
                 f"over {len(payload['inputs_fingerprint']['files'])} file(s). No timestamp "
                 f"is written anywhere in this report, so two runs over identical inputs "
                 f"and identical prior snapshot state produce identical bytes and a "
                 f"regression is a diff. The one exception is `resolution_transitions`, "
                 f"which is stateful by design: it reads the ref-status snapshot the "
                 f"previous run wrote, so a second run in the same output directory "
                 f"legitimately reports history the first one did not have.")
    failed_inputs = payload["input_source"]["failed"]
    if failed_inputs:
        lines.append("")
        lines.append(f"**{len(failed_inputs)} input file(s) failed to load**, which is "
                     f"different from absent:")
        for f in failed_inputs:
            lines.append(f"  - `{f['path']}`: {f['error']}")
    skipped_inputs = payload["input_source"]["skipped_not_a_part"]
    if skipped_inputs:
        lines.append("")
        lines.append(f"{len(skipped_inputs)} file(s) in `tree/` were skipped as not "
                     f"part files, which is different again from failing to load:")
        for f in skipped_inputs:
            lines.append(f"  - `{f['path']}`: {f['error']}")
    lines.append("")
    lines.append("## gates")
    lines.append("")
    lines.append("Thresholds from `config.GATES`. A gate never fires on missing data; "
                 "it records `skipped_no_data` and says why.")
    lines.append("")
    lines.append("| gate | what it answers | threshold | observed | status |")
    lines.append("|---|---|---|---|---|")
    for g in gates:
        mark = {"pass": "pass", "fail": "**FAIL**",
                "skipped_no_data": "skipped, no data",
                "unimplemented": "**UNIMPLEMENTED**"}.get(g.status, g.status)
        lines.append(f"| `{g.name}` | {g.question or ''} | {g.threshold} | "
                     f"{g.observed or '—'} | {mark} |")
    reasons = [g for g in gates if g.reason]
    if reasons:
        lines.append("")
        for g in reasons:
            lines.append(f"- `{g.name}`: {g.reason}")
    for section in sections:
        lines.append("")
        lines.append(f"## {section.name}")
        lines.append("")
        lines.append(f"_{SECTION_SUBTITLES[section.name]}. Status: **{section.status}**"
                     + (f", {section.reason}" if section.reason else "") + "._")
        lines.append("")
        lines.extend(section.md or ["_no output_"])
    lines.append("")
    return "\n".join(lines) + "\n"


def write(ctx: Context, sections: list[Section],
          gates: list[GateResult]) -> tuple[Path, Path, dict[str, Any]]:
    payload = assemble(ctx, sections, gates)
    ctx.eval_dir.mkdir(parents=True, exist_ok=True)
    json_path = ctx.eval_dir / "report.json"
    md_path = ctx.eval_dir / "report.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    md_path.write_text(render_markdown(ctx, sections, gates, payload))
    return json_path, md_path, payload
