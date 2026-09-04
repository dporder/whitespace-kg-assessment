"""Row building: labels, edges, encodings, and what the loader refuses to write."""
from __future__ import annotations

import json

from pipeline.load import associated, export
from pipeline.load.rows import (Rows, concept_rows, dangling_endpoints, dedupe,
                                legislation_rows, merge_key, node_props, term_rows,
                                tree_rows, walk)
from pipeline.schemas import Candidate, GraphEdge, Legislation


def build(small_tree, small_refs, batch="T1"):
    return tree_rows({small_tree.path: small_tree}, {small_tree.path: small_refs},
                     batch_id=batch, document=None)


def edges_of(rows: Rows, type_: str) -> list[GraphEdge]:
    return [e for e in rows.edges if e.type == type_]


def test_every_node_carries_Node_plus_its_kind_label(small_tree, small_refs):
    rows = build(small_tree, small_refs)
    for row in rows.nodes:
        assert row.labels[0] == "Node"
        assert len(row.labels) == 2
    labels = {row.labels[1] for row in rows.nodes}
    assert labels == {"Part", "Heading", "Clause", "Intro", "Item", "Ref"}


def test_contains_and_next_follow_the_tree(small_tree, small_refs):
    rows = build(small_tree, small_refs)
    by_id = {n.id: n for n in walk(small_tree)}
    contains = {(e.src, e.dst) for e in edges_of(rows, "CONTAINS")}
    clause = small_tree.children[0].children[0]
    assert (small_tree.id, small_tree.children[0].id) in contains
    for child in clause.children:
        assert (clause.id, child.id) in contains
    nexts = [(e.src, e.dst) for e in edges_of(rows, "NEXT")]
    assert (clause.children[0].id, clause.children[1].id) in nexts
    assert all(by_id[s].order < by_id[d].order for s, d in nexts)


def test_a_ref_is_contained_by_the_provision_that_cites_it(small_tree, small_refs):
    rows = build(small_tree, small_refs)
    intro = small_tree.children[0].children[0].children[0]
    contains = {(e.src, e.dst) for e in edges_of(rows, "CONTAINS")}
    assert (intro.id, small_refs[0].id) in contains


def test_resolved_refs_get_resolves_to_and_unresolved_ones_do_not(small_tree, small_refs,
                                                                  ):
    rows = build(small_tree, small_refs)
    resolved = edges_of(rows, "RESOLVES_TO")
    assert len(resolved) == 2                     # one internal, one external
    targets = {e.dst for e in resolved}
    assert "legislation/bribery-act-2010" in targets
    assert small_tree.children[0].children[1].id in targets


def test_a_candidate_outside_the_corpus_never_becomes_an_edge(small_tree, small_refs):
    """MERGEing an edge to a path that is not a node would mint the node SPEC 2.2
    forbids a ref from minting. The list survives as a property."""
    small_refs[0].status = "unresolved"
    small_refs[0].target_path = None
    ingested = small_tree.children[0].children[1].path
    small_refs[0].candidates = [Candidate(path="framework-schedule-4", score=0.95,
                                          reason="part not ingested"),
                                Candidate(path=ingested, score=0.4,
                                          reason="ingested")]
    rows = build(small_tree, small_refs)
    candidate_targets = {e.dst for e in edges_of(rows, "CANDIDATE")}
    assert candidate_targets == {small_tree.children[0].children[1].id}
    row = next(r for r in rows.nodes if r.key_value == small_refs[0].id)
    stored = json.loads(row.props["candidates"])
    assert {c["path"] for c in stored} == {"framework-schedule-4", ingested}


def test_bboxes_are_stored_as_json_strings(small_tree, small_refs):
    """Neo4j cannot hold a list of maps; the chat UI's decoder takes this shape."""
    from pipeline.schemas import BBox
    small_tree.bboxes_extent = [BBox(page=1, bbox=(1.0, 2.0, 3.0, 4.0))]
    props = node_props(small_tree)
    assert isinstance(props["bboxes_extent"], str)
    assert json.loads(props["bboxes_extent"])[0]["page"] == 1


def test_a_char_span_survives_as_a_list(small_tree, small_refs):
    rows = build(small_tree, small_refs)
    row = next(r for r in rows.nodes if r.key_value == small_refs[0].id)
    assert row.props["char_span"] == [12, 21]


def test_the_load_batch_is_stamped_and_the_stage_batch_is_kept(small_tree, small_refs):
    rows = build(small_tree, small_refs, batch="T2")
    row = next(r for r in rows.nodes if r.key_value == small_tree.id)
    assert row.props["batch_id"] == "T2"
    assert row.props["source_batch_id"] == "T1"


def test_terms_definitions_and_uses(small_tree, small_refs, small_vocab):
    sites, uses = small_vocab
    by_id = {n.id: n for n in walk(small_tree)}
    rows = term_rows(sites, uses, by_id, batch_id="T1")
    assert {r.key_value for r in rows.nodes} == {"Buyer", "New IPR"}
    buyer = next(r for r in rows.nodes if r.key_value == "Buyer")
    assert buyer.props["aliases"] == ["CCS"]
    defined = edges_of(rows, "DEFINED_IN")
    assert len(defined) == 1 and defined[0].props["scope"] == "document"
    uses_edges = edges_of(rows, "USES_TERM")
    assert len(uses_edges) == 2
    assert all("char_span" in e.props for e in uses_edges)


def test_defined_using_falls_out_of_terms_used_inside_a_definition(small_tree,
                                                                   small_vocab):
    """SPEC 2.3: the vocabulary's own dependency graph, deterministically."""
    sites, uses = small_vocab
    by_id = {n.id: n for n in walk(small_tree)}
    rows = term_rows(sites, uses, by_id, batch_id="T1")
    defined_using = {(e.src, e.dst) for e in edges_of(rows, "DEFINED_USING")}
    assert defined_using == {("Buyer", "New IPR")}


def test_uses_term_is_discriminated_by_char_span():
    a = GraphEdge(type="USES_TERM", src="n1", dst="Buyer",
                  props={"char_span": [1, 5]}, batch_id="T1")
    b = GraphEdge(type="USES_TERM", src="n1", dst="Buyer",
                  props={"char_span": [9, 13]}, batch_id="T1")
    assert merge_key(a) != merge_key(b), "two uses in one node would collapse into one"
    same = GraphEdge(type="CONTAINS", src="n1", dst="n2", props={}, batch_id="T1")
    assert merge_key(same) == merge_key(
        GraphEdge(type="CONTAINS", src="n1", dst="n2", props={"x": 1}, batch_id="T2"))


def test_concepts_are_never_citable_and_members_are_checked(small_tree, small_concepts):
    by_id = {n.id: n for n in walk(small_tree)}
    rows = concept_rows(small_concepts, by_id, batch_id="T1")
    assert rows.nodes[0].props["citable"] is False
    assert rows.nodes[0].props["llm_derived"] is True
    assert len(edges_of(rows, "ABOUT")) == 2
    small_concepts[0].member_node_ids = ["not-a-node"]
    rows = concept_rows(small_concepts, by_id, batch_id="T1")
    assert edges_of(rows, "ABOUT") == []
    assert rows.notes[0]["kind"] == "concept_member_without_node"


def test_associated_term_is_the_share_of_member_provisions_using_the_term(
        small_tree, small_concepts, small_vocab):
    _sites, uses = small_vocab
    by_id = {n.id: n for n in walk(small_tree)}
    rows = associated.build(small_concepts, uses, by_id, batch_id="T1", threshold=0.25)
    edges = {e.dst: e.props for e in rows.edges}
    # both terms are used by 1 of the concept's 2 member provisions
    assert edges["Buyer"]["share"] == 0.5
    assert edges["Buyer"]["llm_derived"] is True
    assert edges["Buyer"]["members_counted"] == 2


def test_associated_term_respects_the_threshold(small_tree, small_concepts, small_vocab):
    _sites, uses = small_vocab
    by_id = {n.id: n for n in walk(small_tree)}
    rows = associated.build(small_concepts, uses, by_id, batch_id="T1", threshold=0.9)
    assert rows.edges == []


def test_associated_term_excludes_members_the_run_does_not_hold(small_tree,
                                                                small_concepts,
                                                                small_vocab):
    _sites, uses = small_vocab
    by_id = {n.id: n for n in walk(small_tree)}
    small_concepts[0].member_node_ids.append("absent-node")
    rows = associated.build(small_concepts, uses, by_id, batch_id="T1", threshold=0.25)
    assert any(n["kind"] == "associated_term_members_missing" for n in rows.notes)
    assert {e.dst: e.props["members_counted"] for e in rows.edges} == {"Buyer": 2,
                                                                      "New IPR": 2}


def test_legislation_nodes_come_from_the_refs_that_cite_them(small_tree, small_refs):
    records = [Legislation(key="legislation/bribery-act-2010", title="Bribery Act",
                           year=2010, instrument_kind="act")]
    rows = legislation_rows({small_tree.path: small_refs}, records, batch_id="T1")
    assert [r.key_value for r in rows.nodes] == ["legislation/bribery-act-2010"]
    assert rows.nodes[0].labels == ["Legislation"]
    assert rows.nodes[0].props["year"] == 2010


def test_a_legislation_key_with_no_record_still_lands_as_a_node(small_tree, small_refs):
    rows = legislation_rows({small_tree.path: small_refs}, [], batch_id="T1")
    assert rows.nodes[0].props["title"] == "Bribery Act"
    assert rows.nodes[0].props["year"] == 2010


def test_dedupe_collapses_one_merge_key_and_says_so():
    rows = Rows(edges=[
        GraphEdge(type="CONTAINS", src="a", dst="b", props={"x": 1}, batch_id="T1"),
        GraphEdge(type="CONTAINS", src="a", dst="b", props={"x": 2}, batch_id="T1"),
    ])
    out, collapsed = dedupe(rows)
    assert len(out.edges) == 1
    assert collapsed[0]["differing_props"] == {"x": (1, 2)}


def test_the_networkx_export_is_built_from_the_same_rows(tmp_path, small_tree,
                                                         small_refs):
    rows = build(small_tree, small_refs)
    rows.nodes.extend(legislation_rows({small_tree.path: small_refs}, [],
                                       batch_id="T1").nodes)
    assert dangling_endpoints(rows) == []
    result = export.write(rows, tmp_path / "graph.json", {"batch_id": "T1"})
    data = json.loads((tmp_path / "graph.json").read_text())
    assert result["nodes"] == len(rows.nodes)
    assert data["graph"]["batch_id"] == "T1"
    assert len(data["nodes"]) == len(rows.nodes)


def test_an_edge_endpoint_with_no_node_row_is_reported(small_tree, small_refs):
    """NetworkX would invent the node and the Neo4j load would drop the edge, so
    the two sinks would disagree about what the graph holds."""
    rows = build(small_tree, small_refs)
    found = dangling_endpoints(rows)
    assert [d["key"] for d in found] == ["legislation/bribery-act-2010"]
