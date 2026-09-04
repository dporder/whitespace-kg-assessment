"""Stage output discovery, with absent and failed kept apart.

Tonight most of the pipeline does not exist. Every loader here answers three
questions separately: was the file there, did it parse, did it validate against
`pipeline/schemas.py`. A missing stage is `absent` and degrades a section to
`no_data`. A present-but-broken stage is `failed` and is a reported error, not
a silent zero.

Input source resolution, in order:
  --input output    read output/<run>/, error if the run has nothing
  --input fixtures  read fixtures/
  --input auto      output/<run>/ when it holds stage output, else fixtures/
The chosen source is stamped on the report so no number is ever read out of
context.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from pipeline.schemas import Concept, DefinitionSite, Node, RefsFile, TermUse

OUTPUT_SOURCE = "output"
FIXTURES_SOURCE = "fixtures"


@dataclass
class Loaded:
    """One input file: where it came from and what happened to it."""
    kind: str                      # tree | refs | definition_sites | term_uses | concepts | profile
    key: str                       # part id, or the stage name for whole-stage files
    path: Optional[Path] = None
    state: str = "absent"          # loaded | absent | failed
    error: Optional[str] = None
    value: Any = None

    @property
    def ok(self) -> bool:
        return self.state == "loaded"

    def as_dict(self) -> dict[str, Any]:
        out = {"kind": self.kind, "key": self.key, "state": self.state}
        if self.path is not None:
            out["path"] = str(self.path)
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Inputs:
    """Everything stage 8 reads, plus the record of what was not there."""
    source: str
    root: Path
    run: str
    scope_parts: list[str] = field(default_factory=list)
    records: list[Loaded] = field(default_factory=list)
    trees: dict[str, Node] = field(default_factory=dict)
    refs: dict[str, list[Node]] = field(default_factory=dict)
    definition_sites: Optional[list[DefinitionSite]] = None
    term_uses: Optional[list[TermUse]] = None
    concepts: Optional[list[Concept]] = None
    profile: Optional[dict] = None

    # -- convenience views ---------------------------------------------------
    def all_refs(self) -> list[Node]:
        return [r for part in sorted(self.refs) for r in self.refs[part]]

    def nodes(self) -> Iterator[tuple[str, Node]]:
        """(part, node) for every node in every loaded tree, preorder."""
        for part in sorted(self.trees):
            yield from ((part, n) for n in walk(self.trees[part]))

    def nodes_by_id(self) -> dict[str, Node]:
        return {n.id: n for _, n in self.nodes()}

    def nodes_by_path(self) -> dict[str, Node]:
        return {n.path: n for _, n in self.nodes()}

    def failures(self) -> list[Loaded]:
        return [r for r in self.records if r.state == "failed"]

    def absences(self) -> list[Loaded]:
        return [r for r in self.records if r.state == "absent"]


def walk(node: Node) -> Iterator[Node]:
    """Preorder walk, children in stored order. Deterministic."""
    yield node
    for child in node.children:
        yield from walk(child)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _load_one(kind: str, key: str, path: Path, parse) -> Loaded:
    rec = Loaded(kind=kind, key=key, path=path)
    if not path.exists():
        return rec
    try:
        rec.value = parse(_read_json(path))
        rec.state = "loaded"
    except Exception as exc:                              # noqa: BLE001 - reported, never raised
        rec.state = "failed"
        rec.error = f"{type(exc).__name__}: {exc}"
    return rec


def has_stage_output(run_dir: Path) -> bool:
    """True when a run directory holds anything stage 8 can read."""
    if not run_dir.exists():
        return False
    for sub in ("tree", "refs", "vocab", "layout"):
        d = run_dir / sub
        if d.is_dir() and any(d.glob("*.json")):
            return True
    return (run_dir / "concepts.json").exists()


def newest_run(output_root: Path) -> Optional[str]:
    """The most recently modified run directory holding stage output."""
    if not output_root.is_dir():
        return None
    runs = [d for d in output_root.iterdir() if d.is_dir() and has_stage_output(d)]
    if not runs:
        return None
    return sorted(runs, key=lambda d: (d.stat().st_mtime, d.name))[-1].name


def discover_parts(source_root: Path, source: str) -> list[str]:
    """Part ids present in the input source, from the stage 2 tree files."""
    tree_dir = source_root / "tree"
    if not tree_dir.is_dir():
        return []
    return sorted(p.stem for p in tree_dir.glob("*.json"))


def load(source: str, source_root: Path, run: str, parts: list[str],
         run_dir: Optional[Path] = None) -> Inputs:
    """Load every stage output stage 8 reads, for the parts in scope.

    Never raises on bad input: a file that will not parse or validate becomes
    a `failed` record and the section that needed it degrades.
    """
    inputs = Inputs(source=source, root=source_root, run=run, scope_parts=list(parts))

    for part in parts:
        rec = _load_one("tree", part, source_root / "tree" / f"{part}.json",
                        lambda d: Node.model_validate(d))
        inputs.records.append(rec)
        if rec.ok:
            inputs.trees[part] = rec.value

        rec = _load_one("refs", part, source_root / "refs" / f"{part}.json",
                        lambda d: RefsFile.model_validate(d))
        inputs.records.append(rec)
        if rec.ok:
            inputs.refs[part] = rec.value.refs

    vocab = source_root / "vocab"
    rec = _load_one("definition_sites", "vocab", vocab / "definition_sites.json",
                    lambda d: [DefinitionSite.model_validate(x) for x in d])
    inputs.records.append(rec)
    if rec.ok:
        inputs.definition_sites = rec.value

    rec = _load_one("term_uses", "vocab", vocab / "term_uses.json",
                    lambda d: [TermUse.model_validate(x) for x in d])
    inputs.records.append(rec)
    if rec.ok:
        inputs.term_uses = rec.value

    rec = _load_one("concepts", "concepts", source_root / "concepts.json",
                    lambda d: [Concept.model_validate(x) for x in d])
    inputs.records.append(rec)
    if rec.ok:
        inputs.concepts = rec.value

    # Stage 0 writes output/profile.json next to the run dirs, not inside one.
    if run_dir is not None:
        for candidate in (run_dir.parent / "profile.json", run_dir / "profile.json"):
            rec = _load_one("profile", "profile", candidate, lambda d: d)
            if rec.state != "absent":
                inputs.records.append(rec)
                if rec.ok:
                    inputs.profile = rec.value
                break
        else:
            inputs.records.append(Loaded(kind="profile", key="profile",
                                         path=run_dir.parent / "profile.json"))
    return inputs
