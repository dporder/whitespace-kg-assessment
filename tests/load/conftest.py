"""Scaffolding for the stage 7 tests: a small graph, and a real Neo4j or a skip.

The Neo4j tests are integration tests against the running instance at
`config.NEO4J`, because a mocked driver would prove that the code calls a mock,
not that MERGE converges. They use their own throwaway batch ids and roll them
back afterwards, so they never disturb whatever else is in the graph.
"""
from __future__ import annotations

import os
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
def part_id() -> str:
    """A part id no real ingestion can collide with.

    This matters more than it looks. `sweep(scope, batch)` deletes everything
    under a path prefix that the batch did not re-assert, so a test that swept
    the scope "core-terms" would delete the real Core Terms graph sitting in the
    same database. It did, once, which is how this fixture came to exist.
    """
    return f"loadtest-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def small_tree(part_id) -> Node:
    """A part with a lead-in, two items and a sibling clause: the real shapes."""
    intro = node(f"{part_id}/9/9.1/intro", "intro", citable=False, order=3,
                 text="Subject to Clause 9.2, each Party must:")
    a = node(f"{part_id}/9/9.1/a", "item", label="(a)", order=4, text="do the thing; and")
    b = node(f"{part_id}/9/9.1/b", "item", label="(b)", order=5, text="do the other.")
    c91 = node(f"{part_id}/9/9.1", "clause", label="9.1", order=2, children=[intro, a, b])
    c92 = node(f"{part_id}/9/9.2", "clause", label="9.2", order=6,
               text="New IPR is owned by the Buyer under the Bribery Act 2010.")
    head = node(f"{part_id}/9", "heading", label="9", order=1, children=[c91, c92],
                title="Intellectual Property Rights")
    return node(part_id, "part", order=0, children=[head], title="Core Terms",
                part_family="core", batch_id="T1")


@pytest.fixture
def statute_key(part_id) -> str:
    """A statute key no real citation can produce.

    `Legislation.key` is a global uniqueness key by design, so fifty mentions
    of one Act meet at one node. That also means a test citing a real statute
    MERGEs onto the real node and a later rollback takes it away with the test
    batch. It did, once, which is how this fixture came to exist.
    """
    return f"legislation/{part_id}-act-1999"


@pytest.fixture
def small_refs(small_tree, statute_key) -> list[Node]:
    intro = small_tree.children[0].children[0].children[0]
    c92 = small_tree.children[0].children[1]
    return [
        ref(intro, (12, 21), status="resolved",
            target=f"{small_tree.path}/9/9.2", order=0),
        ref(c92, (44, 60), status="external", ref_kind="legislation",
            target=statute_key, scope_rule="none",
            resolver="grammar", order=1),
    ]


@pytest.fixture
def term_names(part_id) -> tuple[str, str]:
    """Term.name is a global key too, for the same reason and with the same
    hazard, so the tests coin terms no document defines."""
    return f"Buyer-{part_id}", f"New IPR-{part_id}"


@pytest.fixture
def small_vocab(small_tree, term_names):
    buyer, new_ipr = term_names
    c92 = small_tree.children[0].children[1]
    sites = [DefinitionSite(term=buyer, definition_node_id=c92.id, source="declared",
                            scope="document", aliases=["CCS"])]
    uses = [TermUse(term=buyer, node_id=c92.id, char_span=(25, 30), status="confident",
                    method="exact_longest", definition_used="document"),
            TermUse(term=new_ipr, node_id=c92.id, char_span=(0, 7), status="confident",
                    method="exact_longest", definition_used="document")]
    return sites, uses


@pytest.fixture
def small_concepts(small_tree, part_id):
    c91 = small_tree.children[0].children[0]
    c92 = small_tree.children[0].children[1]
    concept_id = f"concept-ip-{part_id}"        # Concept.id is global too
    return [Concept(id=concept_id, label="intellectual property",
                    scope_path=f"{small_tree.path}/9", member_node_ids=[c91.id, c92.id],
                    relations=[ConceptRelation(src=concept_id, label="relates_to",
                                               dst=concept_id)],
                    confidence=0.7)]


@pytest.fixture
def throwaway_batch() -> str:
    return f"TEST-{uuid.uuid4().hex[:8]}"


FOREIGN_DATA_ENV = "PIPELINE_TEST_REQUIRE_EMPTY_NEO4J"


@pytest.fixture
def graph(throwaway_batch):
    """A live Neo4j, or a skip. Everything this test wrote is rolled back.

    All five worktrees share one database, so these tests take a census of
    everything that is not theirs before they start and prove at teardown that
    none of it was touched. That is stronger than refusing to run: a sweep or a
    rollback with too wide a blast radius fails the test that caused it instead
    of quietly deleting a colleague's graph, which is exactly what happened
    before this fixture existed. Set PIPELINE_TEST_REQUIRE_EMPTY_NEO4J=1 to
    refuse to run at all when the database holds anything else.
    """
    if not Graph.available():
        pytest.skip("Neo4j is not reachable at config.NEO4J")
    g = Graph()
    g.ensure_schema()

    def census() -> tuple[set, set]:
        ids = {r["id"] for r in g.read(
            "MATCH (n) WHERE n.id IS NOT NULL RETURN n.id AS id")}
        keys = {r["k"] for r in g.read(
            "MATCH (n) WHERE n:Term OR n:Legislation OR n:Concept "
            "RETURN coalesce(n.name, n.key, n.id) AS k")}
        return ids, keys

    before_ids, before_keys = census()
    if (before_ids or before_keys) and os.environ.get(FOREIGN_DATA_ENV) == "1":
        g.close()
        pytest.skip(f"{FOREIGN_DATA_ENV}=1 and the database holds "
                    f"{len(before_ids)} node(s) these tests did not create")

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
        after_ids, after_keys = census()
        g.close()
        lost_ids = before_ids - after_ids
        lost_keys = before_keys - after_keys
        assert not lost_ids and not lost_keys, (
            f"this test deleted data it did not create: {len(lost_ids)} node(s) and "
            f"referent(s) {sorted(lost_keys)[:5]}. Every sweep and rollback here must "
            f"be scoped to what the test itself asserted.")
