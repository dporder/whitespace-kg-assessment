"""A disposable copy of `fixtures/` the stage 8 tests can mutate.

`fixtures/` is orchestrator-owned and must not be edited, so every test that
needs a broken tree, a golden label or a second run works on a copy under
tmp_path. The copy is a faithful one: the shipped fixtures are the green
baseline, and a seeded failure is the only difference between a passing run and
a failing one.

This used to inject a `numbering_gap` anomaly on `core-terms/9`, because the
fixture Core Terms tree is an excerpt holding clauses 3 and 9 with 4 to 8 left
out and the shipped tree recorded nothing about it. Master 95f326e records that
anomaly in `fixtures/make_fixtures.py`, so the injection is gone; a test fixture
that quietly repairs its own input is a trap the next reader would have to
discover.
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

    def definition_sites(self) -> list[dict]:
        return json.loads((self.fixtures / "vocab" / "definition_sites.json").read_text())

    def write_definition_sites(self, sites: list[dict]) -> None:
        (self.fixtures / "vocab" / "definition_sites.json").write_text(
            json.dumps(sites, indent=2))

    def drop_definition_site(self, term: str) -> None:
        """Remove a term's definition site, leaving its uses pointing at nothing."""
        sites = self.definition_sites()
        remaining = [s for s in sites if s["term"] != term]
        assert len(remaining) < len(sites), f"no definition site for {term!r} to drop"
        self.write_definition_sites(remaining)

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
    """The shipped fixtures, copied verbatim. They are the green baseline."""
    fixtures = tmp_path / "fixtures"
    shutil.copytree(SHIPPED_FIXTURES, fixtures)
    return Workspace(root=tmp_path, fixtures=fixtures,
                     output=tmp_path / "output", golden=tmp_path / "golden")
