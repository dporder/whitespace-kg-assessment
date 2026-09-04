"""`resolution_transitions`: per batch, unresolved to resolved counts.

SPEC section 3: "After each batch load, stage 3 re runs over refs with status
unresolved and stage 8 records the transitions." That is the second-document
story made measurable. A reference out to a part that has not been ingested
sits unresolved with its candidates kept, and when the target arrives it flips.
This section counts the flips.

Each run writes `eval/ref_status_snapshot.json`, a ref path to status map, and
compares against the newest earlier snapshot it can find. The first run has
nothing to compare against, which is `no_data`, not zero transitions: a report
saying "0 unresolved became resolved" when it simply had no history would be
exactly the confident wrongness this evaluation exists to prevent.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from pipeline.eval.context import Context, LIST_CAP
from pipeline.eval.rates import MEASURED, NO_DATA, Rate, Section, cap

SNAPSHOT_NAME = "ref_status_snapshot.json"


def current_snapshot(ctx: Context) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for part in sorted(ctx.inputs.refs):
        for r in ctx.inputs.refs[part]:
            out[r.path] = {"status": r.status, "batch_id": r.batch_id,
                           "target_path": r.target_path, "resolver": r.resolver,
                           "part": part}
    return out


def find_previous(ctx: Context) -> tuple[Optional[dict], Optional[Path], str]:
    """The newest earlier snapshot, or why there is none."""
    if ctx.previous_snapshot is not None:
        p = ctx.previous_snapshot
        if not p.exists():
            return None, None, f"--previous-snapshot {p} does not exist"
        try:
            return json.loads(p.read_text()).get("refs", {}), p, "explicit --previous-snapshot"
        except Exception as exc:                          # noqa: BLE001
            return None, None, f"{p} could not be read: {type(exc).__name__}: {exc}"

    this_run = ctx.eval_dir / SNAPSHOT_NAME
    candidates = [p for p in ctx.run_dir.parent.glob(f"*/eval/{SNAPSHOT_NAME}")
                  if p != this_run and p.exists()]
    if this_run.exists():
        candidates.append(this_run)          # a rerun of the same run id is still history
    if not candidates:
        return None, None, ("no earlier snapshot under output/*/eval/; this is the first "
                            "observation, so there is no history to count transitions over")
    newest = sorted(candidates, key=lambda p: (p.stat().st_mtime, str(p)))[-1]
    try:
        return json.loads(newest.read_text()).get("refs", {}), newest, "newest earlier snapshot"
    except Exception as exc:                              # noqa: BLE001
        return None, None, f"{newest} could not be read: {type(exc).__name__}: {exc}"


def write_snapshot(ctx: Context, snapshot: dict[str, dict[str, Any]]) -> Path:
    ctx.eval_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.eval_dir / SNAPSHOT_NAME
    path.write_text(json.dumps({"run": ctx.run, "refs": snapshot},
                               indent=2, sort_keys=True) + "\n")
    return path


def build(ctx: Context) -> Section:
    s = Section("resolution_transitions")
    snapshot = current_snapshot(ctx)
    previous, previous_path, note = find_previous(ctx)
    s.data["snapshot"] = {"refs": len(snapshot), "written_to": str(ctx.eval_dir / SNAPSHOT_NAME)}
    s.data["previous"] = {"path": str(previous_path) if previous_path else None, "note": note}
    # The report's inputs fingerprint covers everything that can change the
    # report, and this snapshot can, so tell it which file was read.
    if previous_path is not None:
        ctx.options["previous_snapshot_read"] = str(previous_path)

    if not snapshot:
        s.status = NO_DATA
        s.reason = "no stage 3 refs loaded; nothing to snapshot"
        s.line(f"_{s.reason}_")
        return s

    if previous is None:
        s.status = NO_DATA
        s.reason = note
        s.line(f"_{note}._ Snapshot of **{len(snapshot)}** ref(s) written for the next run.")
        s.line()
        by_batch = Counter((v["batch_id"] or "unbatched", v["status"]) for v in snapshot.values())
        s.line("**Current status, the baseline this run establishes**")
        s.table(["batch", "status", "refs"],
                [[b, st, n] for (b, st), n in sorted(by_batch.items())])
        write_snapshot(ctx, snapshot)
        return s

    matrix: Counter[tuple[str, str, str]] = Counter()
    flips: list[dict[str, Any]] = []
    for path, now in snapshot.items():
        before = previous.get(path)
        batch = now["batch_id"] or "unbatched"
        if before is None:
            matrix[(batch, "(new)", now["status"])] += 1
            continue
        matrix[(batch, before.get("status"), now["status"])] += 1
        if before.get("status") == "unresolved" and now["status"] in ("resolved", "external"):
            flips.append({"path": path, "batch": batch, "from": before.get("status"),
                          "to": now["status"], "target_path": now["target_path"],
                          "resolver": now["resolver"]})
    gone = [p for p in previous if p not in snapshot]

    per_batch: dict[str, dict[str, Any]] = {}
    for (batch, before, after), n in matrix.items():
        cell = per_batch.setdefault(batch, {"transitions": {}, "unresolved_to_resolved": 0,
                                            "refs": 0})
        cell["transitions"][f"{before} -> {after}"] = n
        cell["refs"] += n
        if before == "unresolved" and after in ("resolved", "external"):
            cell["unresolved_to_resolved"] += n

    total_unresolved_before = sum(1 for v in previous.values()
                                  if v.get("status") == "unresolved")
    s.status = MEASURED
    s.data.update({
        "compared_against": str(previous_path),
        "per_batch": per_batch,
        "unresolved_to_resolved": Rate(len(flips), total_unresolved_before).as_dict(),
        "flips": cap(flips, LIST_CAP)[0],
        "flips_not_listed": cap(flips, LIST_CAP)[1],
        "refs_present_before_and_gone_now": cap(gone, LIST_CAP)[0],
    })
    s.line(f"Compared against `{previous_path}` ({note}).")
    s.line()
    s.line(f"Unresolved became resolved: "
           f"**{Rate(len(flips), total_unresolved_before)}** of the refs that were "
           f"unresolved in the previous snapshot.")
    s.line()
    s.table(["batch", "refs", "unresolved -> resolved", "full transition matrix"],
            [[b, v["refs"], v["unresolved_to_resolved"],
              ", ".join(f"{k}: {n}" for k, n in sorted(v["transitions"].items()))]
             for b, v in sorted(per_batch.items())])
    if flips:
        s.line()
        s.table(["ref", "batch", "now points at", "resolver"],
                [[f["path"], f["batch"], f["target_path"], f["resolver"]]
                 for f in cap(flips, LIST_CAP)[0]])
    if gone:
        s.bullet(f"{len(gone)} ref(s) present in the previous snapshot are absent now")
    write_snapshot(ctx, snapshot)
    return s
