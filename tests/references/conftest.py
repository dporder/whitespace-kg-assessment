"""Shared scaffolding for the stage 3 tests: synthetic trees and a fake model.

The trees here are built from `pipeline.schemas` directly rather than copied
from the PDF, for the same reason `fixtures/` is synthetic mimicry: SPEC ground
rule 0 forbids document content anywhere outside `output/`. The structures are
the document's real ones.
"""
from __future__ import annotations

import json
from typing import Optional

import pytest

from pipeline import llm
from pipeline.references.build import Identity
from pipeline.schemas import Node, content_hash, lineage_key, node_id

DOC = "rm6116-test"
VERSION = "v1"


@pytest.fixture
def doc_id() -> str:
    return DOC


@pytest.fixture
def version() -> str:
    return VERSION


@pytest.fixture
def identity() -> Identity:
    """Every synthetic part here is minted under one version, so the per-part
    map is empty and the default covers them all."""
    return Identity(DOC, {}, True, "derived in the fixture", VERSION)


def node(path: str, kind: str, *, text: Optional[str] = None, label: Optional[str] = None,
         title: Optional[str] = None, order: int = 0, page: int = 1,
         children: Optional[list[Node]] = None, **extra) -> Node:
    return Node(
        id=node_id(DOC, VERSION, path), lineage_key=lineage_key(DOC, path),
        content_hash=content_hash(text) if text else None,
        path=path, kind=kind, label=label, title=title, text=text,
        page_start=page, page_end=page, order=order, children=children or [], **extra)


def part(part_id: str, *, title: str, family: str, unit_label: str,
         children: list[Node]) -> Node:
    return node(part_id, "part", title=title, part_family=family,
                unit_label=unit_label, unit_label_source="document",
                children=children, batch_id="B1")


@pytest.fixture
def core_terms() -> Node:
    """Core Terms: a clause with a lead-in and items, and a bare grouping head."""
    return part(
        "core-terms", title="Core Terms", family="core", unit_label="Clause",
        children=[
            node("core-terms/1", "heading", label="1", title="Definitions", order=1,
                 children=[node("core-terms/1/1.2", "clause", label="1.2", order=2,
                                text="The Supplier must comply with Clause 3.1.")]),
            node("core-terms/3", "heading", label="3", title="What has to be provided",
                 order=3,
                 children=[node("core-terms/3/3.1", "heading", label="3.1",
                                title="All outputs", order=4,
                                children=[
                                    node("core-terms/3/3.1/3.1.1", "subclause",
                                         label="3.1.1", order=5,
                                         text="Outputs must meet the Requirement."),
                                    node("core-terms/3/3.1/3.1.2", "subclause",
                                         label="3.1.2", order=6,
                                         text="Outputs carry a warranty."),
                                ])]),
            node("core-terms/4", "clause", label="4", order=7,
                 text="See Clauses 1 to 4 and paragraph 2.1 of Joint Schedule 1."),
            node("core-terms/5", "clause", label="5", order=8,
                 text="Nothing in this Clause limits Table 2 or the Bribery Act 2010."),
        ])


@pytest.fixture
def joint_schedule_1() -> Node:
    """A schedule, which is where the mislabelled "Clause 1.x" case lives."""
    return part(
        "joint-schedule-1", title="Joint Schedule 1 (Definitions)",
        family="joint-schedule", unit_label="Paragraph",
        children=[
            node("joint-schedule-1/1", "heading", label="1", title="Interpretation",
                 order=1,
                 children=[node("joint-schedule-1/1/1.2", "clause", label="1.2", order=2,
                                text="Clause 1.2 applies to this Schedule."),
                           node("joint-schedule-1/1/1.3", "clause", label="1.3", order=3,
                                text="See paragraph 2.1 and Annex 1.")]),
            node("joint-schedule-1/2", "heading", label="2", title="Definitions",
                 order=4,
                 children=[node("joint-schedule-1/2/2.1", "clause", label="2.1", order=5,
                                text="Defined terms have the meaning given.")]),
            node("joint-schedule-1/Annex 1", "heading", label="Annex 1",
                 title="Annex 1 Sources", order=6,
                 children=[node("joint-schedule-1/Annex 1/a1.1", "clause", label="a1.1",
                                order=7, text="Sources are listed here.")]),
        ])


# --------------------------------------------------------------------------
# a fake model, so no test reaches the network
# --------------------------------------------------------------------------
class FakeBlock:
    def __init__(self, text):
        self.type, self.text = "text", text


class FakeMessage:
    def __init__(self, text):
        self.content = [FakeBlock(text)]
        self.stop_reason, self.model, self.usage = "end_turn", "fake", None

    def model_dump(self):
        return {"text": self.content[0].text}


class FakeMessages:
    def __init__(self, replies, error=None):
        self.replies, self.error, self.calls = list(replies), error, []

    def create(self, **payload):
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return FakeMessage(self.replies.pop(0) if self.replies else "{}")


class FakeClient:
    def __init__(self, replies=(), error=None):
        self.messages = FakeMessages(replies, error)


class Refused(Exception):
    status_code = 400

    def __str__(self):
        return ("Error code: 400 - {'error': {'type': 'invalid_request_error', "
                "'message': 'anthropic-workspace-id is required when authenticating "
                "with an identity-linked API key'}}")


def answer(path_or_none: str, confidence: float = 0.8) -> str:
    """A well-formed residue reply, keys in the order the prompt demands."""
    return json.dumps({"considered": [{"path": path_or_none, "for": "x", "against": "y"}],
                       "confidence": confidence, "answer": path_or_none})


@pytest.fixture
def refused():
    """The refusal the live identity-linked key returns today, as a class."""
    return Refused


@pytest.fixture
def answer_json():
    """A well-formed residue reply, keys in the order the prompt demands."""
    return answer


@pytest.fixture
def fake_llm(tmp_path):
    """Every test gets a clean log dir, a clean breaker and no network."""
    llm.set_run_dir(tmp_path / "llmrun")
    llm.reset_breaker()
    llm.set_cache_enabled(True)
    llm.set_sleep(lambda _s: None)

    def install(replies=(), error=None) -> FakeClient:
        client = FakeClient(replies, error)
        llm.set_client(client)
        return client

    yield install
    llm.set_client(None)
    llm.reset_breaker()
