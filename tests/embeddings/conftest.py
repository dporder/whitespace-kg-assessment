"""Scaffolding for the stage 6 tests: a fake OpenAI client and small trees.

The provider is faked rather than called. Tests that hit a paid API are neither
deterministic nor free, and the point of these is the logic around the call:
which altitude a node gets, that the cache stops a second call, that a blocked
provider produces a pending item instead of a record.
"""
from __future__ import annotations

import hashlib
import sys
import types

import pytest

from pipeline.schemas import BBox, Node, content_hash, lineage_key, node_id

DOC = "rm6116-test"
VERSION = "vt"
DIMS = 8


def mk(path: str, kind: str, *, order: int, page: int = 1, **kw) -> Node:
    text = kw.get("text")
    kw.setdefault("bboxes_own", [BBox(page=page, bbox=(72.0, 100.0, 480.0, 115.0))])
    return Node(id=node_id(DOC, VERSION, path), lineage_key=lineage_key(DOC, path),
                content_hash=content_hash(text) if text else None,
                path=path, kind=kind, page_start=page, page_end=page, order=order, **kw)


class FakeEmbeddings:
    def __init__(self, owner):
        self.owner = owner

    def create(self, model, input):                        # noqa: A002
        if self.owner.raises is not None:
            raise self.owner.raises
        self.owner.calls.append((model, list(input)))
        data = []
        for text in input:
            digest = hashlib.sha1(text.encode()).digest()
            vector = [b / 255 for b in digest[:DIMS]]
            data.append(types.SimpleNamespace(embedding=vector))
        return types.SimpleNamespace(data=data)


class FakeOpenAI:
    """Records every call so a test can assert batching and cache behaviour.

    `raises_class` is set on the class rather than an instance, because the
    client is constructed fresh on each `embed`, which is exactly the shape a
    test simulating a provider refusal on a later run needs.
    """
    instances: list["FakeOpenAI"] = []
    raises_class = None

    def __init__(self, api_key=None):
        self.api_key_seen = api_key
        self.calls: list[tuple[str, list[str]]] = []
        self.raises = FakeOpenAI.raises_class
        self.embeddings = FakeEmbeddings(self)
        FakeOpenAI.instances.append(self)


@pytest.fixture
def fake_openai(monkeypatch):
    FakeOpenAI.instances = []
    FakeOpenAI.raises_class = None
    module = types.ModuleType("openai")
    module.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-one")
    yield FakeOpenAI
    FakeOpenAI.raises_class = None


@pytest.fixture
def small_tree() -> Node:
    """A part with a leaf, an intro-plus-items container and a long container."""
    intro = mk("p/1/1.1/intro", "intro", order=3, citable=False,
               text="The Holder shall:")
    a = mk("p/1/1.1/a", "item", order=4, label="(a)", text="keep the register; and")
    b = mk("p/1/1.1/b", "item", order=5, label="(b)", text="notify each change.")
    sub = mk("p/1/1.1", "subclause", order=2, label="1.1", children=[intro, a, b])
    leaf = mk("p/1/1.2", "clause", order=6, label="1.2",
              text="The register shall be kept in the form the Buyer specifies.")
    head = mk("p/1", "heading", order=1, label="1", title="Register duties",
              children=[sub, leaf])
    return mk("p", "part", order=0, title="Framework Schedule 2 (Register)",
              part_family="framework-schedule", children=[head])


@pytest.fixture
def long_container() -> Node:
    """A container whose subtree is far over any sane token budget."""
    children = [mk(f"q/1/1.{i}", "clause", order=i + 1, label=f"1.{i}",
                   text=("The Supplier shall provide the Deliverables in "
                         "accordance with this Contract and the Standards. ") * 6)
                for i in range(1, 12)]
    head = mk("q/1", "heading", order=1, label="1", title="Obligations",
              children=children)
    return mk("q", "part", order=0, title="Framework Schedule 3 (Obligations)",
              part_family="framework-schedule", children=[head])
