"""The audit log. Every merge, sweep, rollback and dedup, with its reason.

SPEC 2.5: "Merges, sweeps, rollbacks and dedups append to an audit log with
batch, affected ids and reason." DESIGN 4 says why: "the graph's history is
reconstructable and a bad decision is findable and reversible rather than
silent."

Append only, one JSON object per line, at `output/<run>/graph/audit.jsonl`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


class Audit:
    def __init__(self, path: Path, *, run: str, batch_id: str):
        self.path = path
        self.run = run
        self.batch_id = batch_id
        self.entries: list[dict] = []

    def record(self, op: str, *, reason: str, affected: Optional[list[Any]] = None,
               counts: Optional[dict] = None, **extra) -> dict:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run": self.run,
            "batch_id": self.batch_id,
            "op": op,
            "reason": reason,
            "affected_count": len(affected) if affected is not None else None,
            "affected": (affected or [])[:200],
            "counts": counts or {},
        }
        entry.update(extra)
        self.entries.append(entry)
        return entry

    def flush(self) -> int:
        if not self.entries:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for entry in self.entries:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        written = len(self.entries)
        self.entries = []
        return written
