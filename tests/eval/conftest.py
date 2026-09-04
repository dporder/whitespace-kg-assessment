"""A disposable copy of `fixtures/` the stage 8 tests can mutate.

`fixtures/` is orchestrator-owned and must not be edited, so every test that
needs a broken tree, a golden label or a second run works on a copy under
tmp_path. The copy is also where the **green baseline** comes from.

The baseline needs one addition the shipped fixtures do not carry. The fixture
Core Terms tree is an excerpt holding clauses 3 and 9 with 4 to 8 left out, so
its top-level siblings really do read "3 then 9" with no anomaly recorded, and
the numbering-gap invariant correctly reports an unexplained violation. A real
parse of the real part would either find 4 to 8 or record the gap. The baseline
records it, exactly as stage 2 would, which is what makes "green, then seed a
failure, then red" an honest demonstration rather than a rigged one.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIPPED_FIXTURES = REPO_ROOT / "fixtures"

EXCERPT_ANOMALY = ("numbering_gap_after_3: the fixture is an excerpt, Core Terms "
                   "clauses 4 to 8 are not included")


def _find(node: dict, path: str) -> Optional[dict]:
    if node.get("path") == path:
        return node
    for child in node.get("children", []):
        found = _find(child, path)
        if found is not None:
            return found
    return None


@dataclass
class Workspace:
    root: Path
    fixtures: Path
    output: Path
    golden: Path

    # -- reading and writing stage output ------------------------------------
    def tree(self, part: str) -> dict:
        return json.loads((self.fixtures / "tree" / f"{part}.json").read_text())

    def write_tree(self, part: str, data: dict) -> None:
        (self.fixtures / "tree" / f"{part}.json").write_text(json.dumps(data, indent=2))

    def refs(self, part: str) -> dict:
        return json.loads((self.fixtures / "refs" / f"{part}.json").read_text())

    def write_refs(self, part: str, data: dict) -> None:
        (self.fixtures / "refs" / f"{part}.json").write_text(json.dumps(data, indent=2))

    def node(self, part: str, path: str) -> dict:
        found = _find(self.tree(part), path)
        assert found is not None, f"{path} not in {part}"
        return found

    def mutate_node(self, part: str, path: str, **changes: Any) -> None:
        data = self.tree(part)
        node = _find(data, path)
        assert node is not None, f"{path} not in {part}"
        node.update(changes)
        self.write_tree(part, data)

    # -- golden labels --------------------------------------------------------
    def label(self, **record: Any) -> None:
        record.setdefault("reviewer", "test")
        record.setdefault("ts", "2026-09-04T00:00:00Z")
        self.golden.mkdir(parents=True, exist_ok=True)
        with (self.golden / "decisions.jsonl").open("a") as fh:
            fh.write(json.dumps(record) + "\n")

    def write_raw_golden(self, text: str, name: str = "decisions.jsonl") -> None:
        self.golden.mkdir(parents=True, exist_ok=True)
        (self.golden / name).write_text(text)

    # -- running the CLI ------------------------------------------------------
    def run(self, *extra: str, use_pdf: bool = False) -> "Run":
        from pipeline.eval.__main__ import main
        args = ["--input", "fixtures",
                "--fixtures-dir", str(self.fixtures),
                "--output-dir", str(self.output),
                "--golden-dir", str(self.golden),
                "--no-llm", "--quiet", *extra]
        if not use_pdf:
            args.append("--no-pdf")
        code = main(args)
        return Run(code=code, workspace=self, run_id=self._run_id(extra))

    @staticmethod
    def _run_id(extra: tuple[str, ...]) -> str:
        if "--run" in extra:
            return extra[extra.index("--run") + 1]
        return "dev"


@dataclass
class Run:
    code: int
    workspace: Workspace
    run_id: str = "dev"

    @property
    def eval_dir(self) -> Path:
        return self.workspace.output / self.run_id / "eval"

    @property
    def report(self) -> dict:
        return json.loads((self.eval_dir / "report.json").read_text())

    @property
    def markdown(self) -> str:
        return (self.eval_dir / "report.md").read_text()

    @property
    def violations(self) -> dict:
        return json.loads((self.eval_dir / "violations.json").read_text())

    def section(self, name: str) -> dict:
        return self.report["sections"][name]

    def gate(self, name: str) -> dict:
        return next(g for g in self.report["gates"]["results"] if g["gate"] == name)

    def failed_gates(self) -> list[str]:
        return [g["gate"] for g in self.report["gates"]["results"]
                if g["status"] in ("fail", "unimplemented")]


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    """A green baseline: the shipped fixtures plus the excerpt anomaly."""
    fixtures = tmp_path / "fixtures"
    shutil.copytree(SHIPPED_FIXTURES, fixtures)
    ws = Workspace(root=tmp_path, fixtures=fixtures,
                   output=tmp_path / "output", golden=tmp_path / "golden")
    data = ws.tree("core-terms")
    node = _find(data, "core-terms/9")
    node.setdefault("anomalies", []).append(EXCERPT_ANOMALY)
    ws.write_tree("core-terms", data)
    return ws


@pytest.fixture
def shipped_workspace(tmp_path: Path) -> Workspace:
    """The shipped fixtures exactly as committed, no baseline correction."""
    fixtures = tmp_path / "fixtures"
    shutil.copytree(SHIPPED_FIXTURES, fixtures)
    return Workspace(root=tmp_path, fixtures=fixtures,
                     output=tmp_path / "output", golden=tmp_path / "golden")
