"""Scaffolding for the stage 5 tests.

The model is faked. What these tests are about is everything around the call:
which units are scanned, that a concept cannot claim a provision that does not
exist, that a label colliding with a declared Term is never minted, and that
near duplicates collapse with a log.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from pipeline.schemas import BBox, Node, content_hash, lineage_key, node_id

DOC = "rm6116-test"
VERSION = "vt"


def mk(path: str, kind: str, *, order: int, page: int = 1, **kw) -> Node:
    text = kw.get("text")
    kw.setdefault("bboxes_own", [BBox(page=page, bbox=(72.0, 100.0, 480.0, 115.0))])
    return Node(id=node_id(DOC, VERSION, path), lineage_key=lineage_key(DOC, path),
                content_hash=content_hash(text) if text else None,
                path=path, kind=kind, page_start=page, page_end=page, order=order, **kw)


class FakeClaude:
    """Returns a scripted reply per call, and records every prompt."""

    def __init__(self, replies):
        self.prompts: list[tuple[str, str]] = []
        self._replies = replies

    def complete(self, task: str, prompt: str) -> str:
        self.prompts.append((task, prompt))
        if callable(self._replies):
            return self._replies(task, prompt)
        if isinstance(self._replies, list):
            return self._replies[min(len(self.prompts) - 1, len(self._replies) - 1)]
        return self._replies


def install_llm(monkeypatch, fake: FakeClaude) -> None:
    module = types.ModuleType("pipeline.llm")
    module.complete = fake.complete
    monkeypatch.setitem(sys.modules, "pipeline.llm", module)


def reply(*concepts) -> str:
    return json.dumps({"concepts": list(concepts)})


def concept(label, confidence, provisions, relations=None) -> dict:
    return {"label": label, "confidence": confidence, "provisions": list(provisions),
            "relations": relations or []}


@pytest.fixture
def two_part_trees():
    """A definitions schedule and a clause part, so a term collision is real."""
    from pipeline.vocabulary import treeio

    intro = mk("defs/1/intro", "intro", order=1, citable=False,
               text="In each Contract, unless the context otherwise requires, the "
                    "following words shall have the following meanings:")
    label = mk("defs/1/table/0/0", "cell", order=3, row=0, col=0, cell_role="label",
               role_confidence=0.99, text='"Exit Management"')
    value = mk("defs/1/table/0/1", "cell", order=4, row=0, col=1, cell_role="value",
               role_confidence=0.99,
               text="means the activities on expiry or termination of a Contract;")
    label2 = mk("defs/1/table/1/0", "cell", order=5, row=1, col=0, cell_role="label",
                role_confidence=0.99, text='"Widget"')
    value2 = mk("defs/1/table/1/1", "cell", order=6, row=1, col=1, cell_role="value",
                role_confidence=0.99, text="means an item supplied under a Contract;")
    table = mk("defs/1/table", "table", order=2, n_rows=2, n_cols=2,
               children=[label, value, label2, value2])
    defs_head = mk("defs/1", "heading", order=1, label="1", title="Definitions",
                   children=[intro, table])
    defs = mk("defs", "part", order=0, title="Joint Schedule 1 (Definitions)",
              part_family="joint-schedule", children=[defs_head])

    a = mk("clauses/1/1.1", "clause", order=2, label="1.1",
           text="A Contract may be terminated on notice where the Supplier is "
                "insolvent.")
    b = mk("clauses/1/1.2", "clause", order=3, label="1.2",
           text="On termination the Supplier shall hand over the Widget register.")
    head1 = mk("clauses/1", "heading", order=1, label="1", title="Termination",
               children=[a, b])
    c = mk("clauses/2/2.1", "clause", order=5, label="2.1",
           text="The Supplier shall keep the register in the form the Buyer "
                "specifies.")
    head2 = mk("clauses/2", "heading", order=4, label="2", title="Records",
               children=[c])
    clauses = mk("clauses", "part", order=0, title="Core Terms", part_family="core",
                 children=[head1, head2])
    return treeio.Trees(source="test", root=None, run="t",
                        parts={"defs": defs, "clauses": clauses}, files={})
