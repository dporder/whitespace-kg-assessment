"""golden/decisions.jsonl, the append-only record of human verdicts.

One JSON object per line, appended under a lock, never rewritten. Stage 8
consumes the file as labels, so the shape is a contract: see review-ui/README.md
for the field table and worked examples.

    {"kind": "ref",     "path": "<ref path>",            "verdict": "approve",
     "chosen_candidate": "<path>",  "reviewer": "dan", "ts": "...Z"}
    {"kind": "term",    "node_id": "<sha1>", "char_span": [0, 21],
     "verdict": "reject", "reviewer": "dan", "ts": "...Z"}
    {"kind": "anomaly", "node_id": "<sha1>", "anomaly": "<the recorded string>",
     "verdict": "approve", "reviewer": "dan", "ts": "...Z"}
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as pipeline_config        # noqa: E402  repo-root config.py

KINDS = ("ref", "term", "anomaly")
VERDICTS = ("approve", "reject")

_lock = threading.Lock()


def path() -> Path:
    return pipeline_config.GOLDEN / "decisions.jsonl"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate(d: dict) -> dict:
    """Raise ValueError unless `d` is a well-formed decision. Returns it."""
    if not isinstance(d, dict):
        raise ValueError("decision must be an object")
    kind = d.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if d.get("verdict") not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {d.get('verdict')!r}")
    if not d.get("reviewer"):
        raise ValueError("reviewer is required")
    if not d.get("ts"):
        raise ValueError("ts is required")

    if kind == "ref":
        if not d.get("path"):
            raise ValueError("a ref decision needs the ref's path")
        if d.get("chosen_candidate") is not None and d["verdict"] != "approve":
            raise ValueError("chosen_candidate only belongs on an approve")
    elif kind == "term":
        if not d.get("node_id"):
            raise ValueError("a term decision needs node_id")
        span = d.get("char_span")
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or not all(isinstance(v, int) for v in span)):
            raise ValueError("a term decision needs char_span as [start, end]")
    else:                                   # anomaly
        if not d.get("node_id"):
            raise ValueError("an anomaly decision needs node_id")
        if not d.get("anomaly"):
            raise ValueError("an anomaly decision needs the anomaly string it answers")
    return d


def target_key(d: dict) -> str:
    """The queue-row id a decision answers, so a row can show its verdict."""
    if d["kind"] == "ref":
        return d["path"]
    if d["kind"] == "term":
        s = d["char_span"]
        return f"{d['node_id']}:{s[0]}-{s[1]}"
    return f"{d['node_id']}#{d.get('anomaly_index', 0)}"


def append(decision: dict, reviewer: str = "unknown") -> dict:
    """Validate, stamp and append one decision. Returns the stored record."""
    d = dict(decision)
    d.setdefault("reviewer", reviewer)
    d.setdefault("ts", now())
    if d["kind"] == "ref":
        d.pop("node_id", None)
        d.pop("char_span", None)
        if d.get("chosen_candidate") in ("", None):
            d.pop("chosen_candidate", None)
    validate(d)

    line = json.dumps(d, ensure_ascii=False, sort_keys=True)
    p = path()
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
    return d


def read_all() -> list[dict]:
    p = path()
    if not p.exists():
        return []
    out = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p}:{i} is not valid JSON: {exc}") from exc
    return out


def decisions_by_target() -> dict[str, dict]:
    """Latest decision per queue row. Later lines win, the file is append-only."""
    out: dict[str, dict] = {}
    for d in read_all():
        try:
            out[target_key(d)] = d
        except (KeyError, TypeError):
            continue
    return out


def summary() -> dict:
    rows = read_all()
    by_kind: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    for d in rows:
        by_kind[d.get("kind", "?")] = by_kind.get(d.get("kind", "?"), 0) + 1
        by_verdict[d.get("verdict", "?")] = by_verdict.get(d.get("verdict", "?"), 0) + 1
    return {
        "path": str(path()),
        "exists": path().exists(),
        "count": len(rows),
        "by_kind": by_kind,
        "by_verdict": by_verdict,
        "recent": rows[-5:][::-1],
    }
