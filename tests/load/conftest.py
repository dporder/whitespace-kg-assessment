"""Scaffolding for the stage 7 tests: a small graph, and a real Neo4j or a skip.

The Neo4j tests are integration tests against the running instance at
`config.NEO4J`, because a mocked driver would prove that the code calls a mock,
not that MERGE converges. They use their own throwaway batch ids and roll them
back afterwards, so they never disturb whatever else is in the graph.
"""
from __future__ import annotations

import uuid
from typing import Optional

import pytest

from pipeline.load.neo4j_loader import Graph
from pipeline.schemas import (Concept, ConceptRelation, DefinitionSite, Node, TermUse,
                              content_hash, lineage_key, node_id)

DOC = "rm6116-loadtest"
VERSION = "v1"


def node(path: str, kind: str, *, text: Optional[str] = None, label: Optional[str] = None,
         order: int = 0, page: int = 1, children=None, **extra) -> Node:
    return Node(id=node_id(DOC, VERSION, path), lineage_key=lineage_key(DOC, path),
                content_hash=content_hash(text) if text else None,
                path=path, kind=kind, label=label, text=text, page_start=page,
                page_end=page, order=order, children=children or [], **extra)


def ref(parent: Node, span: tuple[int, int], *, status: str, ref_kind: str = "clause",
        target: Optional[str] = None, scope_rule: str = "js1_1.3.8",
        resolver: str = "scope", order: int = 0, batch_id: str = "T1",
        candidates=None) -> Node:
    path = f"{parent.path}/ref@{span[0]}-{span[1]}"
    return Node(id=node_id(DOC, VERSION, path), lineage_key=lineage_key(DOC, path),
                path=path, kind="ref", citable=False,
                text=(parent.text or "")[span[0]:span[1]],
                page_start=parent.page_start, page_end=parent.page_start, order=order,
                batch_id=batch_id, char_span=span, ref_kind=ref_kind,
                scope_rule=scope_rule, status=status, target_path=target,
                resolver=resolver, candidates=candidates or [])


@pytest.fixture
def small_tree() -> Node:
    """A part with a lead-in, two items and a sibling clause: the real shapes."""
    intro = node("core-terms/9/9.1/intro", "intro", citable=False, order=3,
                 text="Subject to Clause 9.2, each Party must:")
    a = node("core-terms/9/9.1/a", "item", label="(a)", order=4, text="do the thing; and")
    b = node("core-terms/9/9.1/b", "item", label="(b)", order=5, text="do the other.")
    c91 = node("core-terms/9/9.1", "clause", label="9.1", order=2, children=[intro, a, b])
    c92 = node("core-terms/9/9.2", "clause", label="9.2", order=6,
               text="New IPR is owned by the Buyer under the Bribery Act 2010.")
    head = node("core-terms/9", "heading", label="9", order=1, children=[c91, c92],
                title="Intellectual Property Rights")
    return node("core-terms", "part", order=0, children=[head], title="Core Terms",
                part_family="core", batch_id="T1")


@pytest.fixture
def small_refs(small_tree) -> list[Node]:
    intro = small_tree.children[0].children[0].children[0]
    c92 = small_tree.children[0].children[1]
    return [
        ref(intro, (12, 21), status="resolved", target="core-terms/9/9.2", order=0),
        ref(c92, (44, 60), status="external", ref_kind="legislation",
            target="legislation/bribery-act-2010", scope_rule="none",
            resolver="grammar", order=1),
    ]


@pytest.fixture
def small_vocab(small_tree):
    c92 = small_tree.children[0].children[1]
    sites = [DefinitionSite(term="Buyer", definition_node_id=c92.id, source="declared",
                            scope="document", aliases=["CCS"])]
    uses = [TermUse(term="Buyer", node_id=c92.id, char_span=(25, 30), status="confident",
                    method="exact_longest", definition_used="document"),
            TermUse(term="New IPR", node_id=c92.id, char_span=(0, 7), status="confident",
                    method="exact_longest", definition_used="document")]
    return sites, uses


@pytest.fixture
def small_concepts(small_tree):
    c91 = small_tree.children[0].children[0]
    c92 = small_tree.children[0].children[1]
    return [Concept(id="concept-ip", label="intellectual property",
                    scope_path="core-terms/9", member_node_ids=[c91.id, c92.id],
                    relations=[ConceptRelation(src="concept-ip", label="relates_to",
                                               dst="concept-ip")],
                    confidence=0.7)]


@pytest.fixture
def throwaway_batch() -> str:
    return f"TEST-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def graph(throwaway_batch):
    """A live Neo4j, or a skip. Everything this test wrote is rolled back."""
    if not Graph.available():
        pytest.skip("Neo4j is not reachable at config.NEO4J")
    g = Graph()
    g.ensure_schema()
    created: list[str] = [throwaway_batch]
    g.also_rollback = created.append          # tests add their own batch ids
    try:
        yield g
    finally:
        for batch in created:
            try:
                g.rollback(batch)
            except Exception:                             # noqa: BLE001
                pass
        g.close()
