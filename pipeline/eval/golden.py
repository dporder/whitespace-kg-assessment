"""Reader for the hand labels in `golden/`. The contract is GOLDEN_FORMAT.md.

Append-only JSONL, last record per subject wins. Nothing here assumes how many
labels exist: an empty golden set is `no_data`, not a perfect score, and ten
thousand labels take the same code path as ten.

A malformed line is counted and reported with its file and line number rather
than crashing the run or being dropped. An unrecognised verdict is likewise
surfaced, because a label the harness silently ignores is a label the reviewer
believes was counted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REF_KIND, TERM_KIND, ANOMALY_KIND = "ref", "term", "anomaly"

VERDICTS: dict[str, set[str]] = {
    REF_KIND: {"target", "unresolvable", "not_a_reference"},
    TERM_KIND: {"use", "not_a_use"},
    ANOMALY_KIND: {"agree", "parser_wrong", "outline_wrong", "both_differ",
                   "confirmed", "rejected"},
}
TRIAGE_VERDICTS = ("agree", "parser_wrong", "outline_wrong", "both_differ")
NODE_ANOMALY_VERDICTS = ("confirmed", "rejected")

# Verdicts that assert what the right answer *is*, so the record must name it.
# Without this the harness has two bad options and took a different one for each
# kind: score a label defect as a parser failure (refs), or fall back to the
# pipeline's own answer and grade it against itself (terms). Both are worse than
# refusing the record, so a verdict here without `chosen_candidate` is malformed.
REQUIRES_CHOSEN_CANDIDATE: dict[str, set[str]] = {
    REF_KIND: {"target"},
    TERM_KIND: {"use"},
}

# A node can carry several anomalies, and a verdict on one is not a verdict on
# the others, so the index is part of the subject. Only the node-anomaly
# verdicts need it: the outline triage verdicts are keyed by the queue id this
# harness prints, which names no node anomaly.
REQUIRES_ANOMALY_INDEX: dict[str, set[str]] = {
    ANOMALY_KIND: set(NODE_ANOMALY_VERDICTS),
}

_REF_PATH_RE = re.compile(r"^(?P<parent>.+)/ref@(?P<start>\d+)-(?P<end>\d+)$")


@dataclass
class GoldenRecord:
    kind: str
    verdict: str
    path: Optional[str] = None
    node_id: Optional[str] = None
    char_span: Optional[tuple[int, int]] = None
    chosen_candidate: Optional[str] = None
    anomaly_index: Optional[int] = None
    reviewer: Optional[str] = None
    ts: Optional[str] = None
    note: Optional[str] = None
    source_file: Optional[str] = None
    line_no: Optional[int] = None

    @property
    def parent_path(self) -> Optional[str]:
        """The node whose text the span belongs to, when the subject is a ref path."""
        if self.path:
            m = _REF_PATH_RE.match(self.path)
            if m:
                return m.group("parent")
        return self.path

    @property
    def span(self) -> Optional[tuple[int, int]]:
        if self.path:
            m = _REF_PATH_RE.match(self.path)
            if m:
                return (int(m.group("start")), int(m.group("end")))
        return self.char_span

    @property
    def subject(self) -> tuple:
        """Identity for last-record-wins. A ref path and an equivalent
        node_id+char_span are deliberately different subjects: the harness
        resolves node ids against the trees before comparing, in the sections.

        For anomaly records the index is part of the subject, so two anomalies
        on one node hold two verdicts instead of the second silently replacing
        the first."""
        return (self.kind, self.path, self.node_id, self.span, self.anomaly_index)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "verdict": self.verdict}
        for f in ("path", "node_id", "chosen_candidate", "anomaly_index",
                  "reviewer", "ts"):
            v = getattr(self, f)
            if v is not None:
                out[f] = v
        if self.char_span is not None:
            out["char_span"] = list(self.char_span)
        return out


@dataclass
class GoldenSet:
    state: str = "absent"                       # loaded | absent | failed
    directory: Optional[str] = None
    files: list[str] = field(default_factory=list)
    records: list[GoldenRecord] = field(default_factory=list)
    malformed: list[dict[str, Any]] = field(default_factory=list)
    unknown_verdicts: list[dict[str, Any]] = field(default_factory=list)
    superseded: int = 0
    reviewers: list[str] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[GoldenRecord]:
        return [r for r in self.records if r.kind == kind]

    @property
    def empty(self) -> bool:
        return not self.records

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "directory": self.directory,
            "files": self.files,
            "labels_total": len(self.records),
            "labels_by_kind": {k: len(self.of_kind(k)) for k in
                               (REF_KIND, TERM_KIND, ANOMALY_KIND)},
            "superseded_by_later_record": self.superseded,
            "reviewers": self.reviewers,
            "malformed_lines": self.malformed,
            "unrecognised_verdicts": self.unknown_verdicts,
        }


def _coerce_index(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _coerce_span(value: Any) -> Optional[tuple[int, int]]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (int(value[0]), int(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def load(directory: Path) -> GoldenSet:
    """Read every *.jsonl in the golden directory, decisions.jsonl first.

    Reading sibling label files as well as decisions.jsonl means a hand-written
    starter set and the review UI's live output are scored the same way, which
    is how the set is supposed to grow (EVALUATION.md section 3).
    """
    out = GoldenSet(directory=str(directory))
    if not directory.is_dir():
        return out
    files = sorted(directory.glob("*.jsonl"),
                   key=lambda p: (p.name != "decisions.jsonl", p.name))
    if not files:
        out.files = []
        return out

    by_subject: dict[tuple, GoldenRecord] = {}
    order: list[tuple] = []
    for path in files:
        out.files.append(str(path))
        try:
            lines = path.read_text().splitlines()
        except Exception as exc:                          # noqa: BLE001
            out.state, out.malformed = "failed", out.malformed + [
                {"file": str(path), "line": None, "error": f"{type(exc).__name__}: {exc}"}]
            continue
        for i, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                out.malformed.append({"file": path.name, "line": i, "error": str(exc)})
                continue
            if not isinstance(raw, dict):
                out.malformed.append({"file": path.name, "line": i,
                                      "error": "line is not a JSON object"})
                continue
            kind = raw.get("kind")
            verdict = raw.get("verdict")
            if kind not in VERDICTS:
                out.malformed.append({"file": path.name, "line": i,
                                      "error": f"unknown kind {kind!r}"})
                continue
            if not raw.get("path") and not raw.get("node_id"):
                out.malformed.append({"file": path.name, "line": i,
                                      "error": "record identifies no subject "
                                               "(needs path, or node_id + char_span)"})
                continue
            rec = GoldenRecord(
                kind=kind, verdict=str(verdict),
                path=raw.get("path"), node_id=raw.get("node_id"),
                char_span=_coerce_span(raw.get("char_span")),
                chosen_candidate=raw.get("chosen_candidate"),
                anomaly_index=_coerce_index(raw.get("anomaly_index")),
                reviewer=raw.get("reviewer"), ts=raw.get("ts"),
                note=raw.get("note"), source_file=path.name, line_no=i,
            )
            if verdict not in VERDICTS[kind]:
                out.unknown_verdicts.append({"file": path.name, "line": i,
                                             "kind": kind, "verdict": verdict})
                continue
            if verdict in REQUIRES_CHOSEN_CANDIDATE.get(kind, ()) \
                    and not rec.chosen_candidate:
                out.malformed.append({
                    "file": path.name, "line": i,
                    "error": f"{kind}/{verdict} requires chosen_candidate, naming the "
                             f"correct answer. Without it this record cannot be scored: "
                             f"the harness will not grade the pipeline against its own "
                             f"output, nor count a label defect as a pipeline error."})
                continue
            if verdict in REQUIRES_ANOMALY_INDEX.get(kind, ()) \
                    and rec.anomaly_index is None:
                out.malformed.append({
                    "file": path.name, "line": i,
                    "error": f"{kind}/{verdict} requires anomaly_index: a node may carry "
                             f"several anomalies and a verdict on one is not a verdict on "
                             f"the others."})
                continue
            if rec.subject in by_subject:
                out.superseded += 1
            else:
                order.append(rec.subject)
            by_subject[rec.subject] = rec

    out.records = [by_subject[s] for s in order]
    out.reviewers = sorted({r.reviewer for r in out.records if r.reviewer})
    if out.state != "failed":
        out.state = "loaded"
    return out
