"""golden/decisions.jsonl, the append-only record of human verdicts.

The vocabulary is NOT ours to choose. It is pinned in SPEC section 6 and
elaborated in `pipeline/eval/GOLDEN_FORMAT.md`, whose reference reader is
`pipeline/eval/golden.py`. This module writes what that reader loads, and
`tests/review_ui/test_decisions.py` pins each verdict against the same tables.

    ref      target | unresolvable | not_a_reference
    term     use | not_a_use
    anomaly  confirmed | rejected            (node anomaly readings)
             agree | parser_wrong | outline_wrong | both_differ   (outline triage)

`chosen_candidate` is required on `ref`/`target` (the accepted target path) and
on `term`/`use` (the governing term, which may differ from the matched one in an
alias collision). It is refused on verdicts that have no use for it.

Subject identity, which decides last-record-wins in the harness, is
`(kind, path, node_id, span)`. An anomaly has no span, so two anomalies on one
node would collide; `anomaly_index` distinguishes them and is required here.

One JSON object per line, appended under a lock, never rewritten.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as pipeline_config        # noqa: E402  repo-root config.py

KINDS = ("ref", "term", "anomaly")

# The verdict vocabularies, copied from GOLDEN_FORMAT.md. Any drift here is a
# label the harness will count as unrecognised, so the tests compare these sets
# against pipeline/eval/golden.py directly when that module is importable.
VERDICTS: dict[str, tuple[str, ...]] = {
    "ref": ("target", "unresolvable", "not_a_reference"),
    "term": ("use", "not_a_use"),
    "anomaly": ("confirmed", "rejected",
                "agree", "parser_wrong", "outline_wrong", "both_differ"),
}

# Verdicts that carry chosen_candidate, and what it holds.
NEEDS_CANDIDATE = {("ref", "target"): "the accepted target path",
                   ("term", "use"): "the governing term"}

# Demo and test flows set this so a decisions.jsonl inside the repo always
# holds real reviewer verdicts (SPEC section 6).
PATH_ENV = "RM6116_DECISIONS_PATH"

_lock = threading.Lock()


def path() -> Path:
    override = os.environ.get(PATH_ENV)
    if override:
        return Path(override)
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

    verdict = d.get("verdict")
    if verdict not in VERDICTS[kind]:
        raise ValueError(
            f"verdict for kind {kind!r} must be one of {VERDICTS[kind]}, got {verdict!r}"
        )
    if not d.get("reviewer"):
        raise ValueError("reviewer is required")
    if not d.get("ts"):
        raise ValueError("ts is required")

    holds = NEEDS_CANDIDATE.get((kind, verdict))
    if holds:
        if not d.get("chosen_candidate"):
            raise ValueError(f"{kind}/{verdict} requires chosen_candidate, {holds}")
    elif d.get("chosen_candidate") is not None:
        raise ValueError(f"chosen_candidate has no meaning on {kind}/{verdict}")

    if kind == "ref":
        if not d.get("path"):
            raise ValueError("a ref decision needs the ref's path")
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
        if not isinstance(d.get("anomaly_index"), int) or isinstance(d.get("anomaly_index"), bool):
            raise ValueError(
                "an anomaly decision needs anomaly_index as an int: it is part of "
                "the subject, so without it two anomalies on one node supersede each other"
            )
    return d


def target_key(d: dict) -> str:
    """The queue-row id a decision answers, matching the harness's subject."""
    if d["kind"] == "ref":
        return d["path"]
    if d["kind"] == "term":
        s = d["char_span"]
        return f"{d['node_id']}:{s[0]}-{s[1]}"
    return f"{d['node_id']}#{d['anomaly_index']}"


def append(decision: dict, reviewer: str | None = None) -> dict:
    """Validate, stamp and append one decision. Returns the stored record."""
    d = dict(decision)
    if reviewer and not d.get("reviewer"):
        d["reviewer"] = reviewer
    d.setdefault("ts", now())
    if d.get("chosen_candidate") in ("", None):
        d.pop("chosen_candidate", None)
    if d.get("kind") == "ref":
        d.pop("node_id", None)
        d.pop("char_span", None)
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
        "vocabulary": {k: list(v) for k, v in VERDICTS.items()},
        "recent": rows[-5:][::-1],
    }
