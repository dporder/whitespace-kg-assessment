"""The three duties SPEC 2.5 says must exist and be tested, against real Neo4j.

`rollback(batch_id)` removes a batch completely. `sweep(scope, batch_id)`
deletes anything in scope carrying an earlier batch tag this batch did not
re-assert, which is what makes a rerun converge on state rather than only
avoiding duplicates. `salience()` recomputes breadth times log-damped frequency.

These run against the instance at `config.NEO4J` and skip when it is not
reachable. A mocked driver would only prove that the code calls a mock; MERGE
convergence is a property of the database, so it is tested in the database.
Every test uses its own throwaway batch id and rolls it back afterwards.
"""
from __future__ import annotations

import math

import pytest

from pipeline.load import salience as salience_mod
from pipeline.load.neo4j_loader import Graph, edge_merge, node_merge
from pipeline.load.rows import legislation_rows, term_rows, tree_rows, walk


def load(graph: Graph, tree, refs, batch, sites=None, uses=None):
    rows = tree_rows({tree.path: tree}, {tree.path: refs}, batch_id=batch,
                     document=None)
    rows.nodes.extend(legislation_rows({tree.path: refs}, [], batch_id=batch).nodes)
    if sites is not None or uses is not None:
        by_id = {n.id: n for n in walk(tree)}
        terms = term_rows(sites or [], uses or [], by_id, batch_id=batch)
        rows.nodes.extend(terms.nodes)
        rows.edges.extend(terms.edges)
    graph.merge_nodes(rows.nodes)
    graph.merge_edges(rows.edges)
    return rows


# --------------------------------------------------------------------------
# the Cypher builders, which are the only place a name is interpolated
# --------------------------------------------------------------------------
def test_only_known_labels_and_types_can_reach_cypher():
    with pytest.raises(ValueError):
        node_merge(["Node", "Clause; DROP"], "id")
    with pytest.raises(ValueError):
        edge_merge("DELETE_EVERYTHING", False)
    with pytest.raises(ValueError):
        node_merge(["Node", "Clause"], "id; MATCH (n) DETACH DELETE n")


def test_a_single_label_does_not_produce_broken_cypher():
    assert "SET n += row.props" in node_merge(["Term"], "name")
    assert "SET n:Clause, n += row.props" in node_merge(["Node", "Clause"], "id")


def test_uses_term_merges_on_its_char_span():
    query = edge_merge("USES_TERM", discriminated=True)
    assert "{char_span: row.char_span}" in query
    assert "{char_span" not in edge_merge("CONTAINS", discriminated=False)


# --------------------------------------------------------------------------
# against the database
# --------------------------------------------------------------------------
def test_the_schema_is_constraints_first(graph):
    graph.ensure_schema()
    constraints = graph.read("SHOW CONSTRAINTS YIELD name RETURN collect(name) AS names")
    names = set(constraints[0]["names"])
    assert {"node_id", "term_name", "legislation_key", "concept_id"} <= names
    indexes = graph.read("SHOW INDEXES YIELD name RETURN collect(name) AS names")
    assert {"node_lineage_key", "node_label"} <= set(indexes[0]["names"])


def test_a_rerun_updates_instead_of_growing_a_twin(graph, small_tree, small_refs,
                                                   throwaway_batch):
    load(graph, small_tree, small_refs, throwaway_batch)
    first = graph.counts(throwaway_batch)
    load(graph, small_tree, small_refs, throwaway_batch)
    second = graph.counts(throwaway_batch)
    assert first["nodes_in_batch"] == second["nodes_in_batch"]
    assert first["relationships_in_batch"] == second["relationships_in_batch"]
    assert first["nodes_in_batch"] > 0


def test_two_uses_of_one_term_in_one_node_stay_two_edges(graph, small_tree, small_refs,
                                                         small_vocab, throwaway_batch):
    """The discriminating prop SPEC 2.5 names: without it these collapse to one."""
    sites, uses = small_vocab
    load(graph, small_tree, small_refs, throwaway_batch, sites, uses)
    load(graph, small_tree, small_refs, throwaway_batch, sites, uses)
    rows = graph.read(
        "MATCH ()-[r:USES_TERM]->(t:Term) WHERE r.batch_id = $b "
        "RETURN count(r) AS n", b=throwaway_batch)
    assert rows[0]["n"] == 2


def test_rollback_removes_a_batch_completely(graph, small_tree, small_refs,
                                             throwaway_batch):
    load(graph, small_tree, small_refs, throwaway_batch)
    before = graph.counts(throwaway_batch)
    assert before["nodes_in_batch"] > 0 and before["relationships_in_batch"] > 0
    result = graph.rollback(throwaway_batch)
    after = graph.counts(throwaway_batch)
    assert after["nodes_in_batch"] == 0
    assert after["relationships_in_batch"] == 0
    assert result["nodes_deleted"] == before["nodes_in_batch"]
    assert result["nodes_remaining"] == 0


def test_rollback_leaves_other_batches_alone(graph, small_tree, small_refs,
                                             throwaway_batch):
    other = f"{throwaway_batch}-other"
    graph.also_rollback(other)
    load(graph, small_tree, small_refs, throwaway_batch)
    kept = graph.counts(throwaway_batch)["nodes_in_batch"]
    graph.run("CREATE (n:Node {id: $id, path: 'other-part', batch_id: $b})",
              id=f"{other}-node", b=other)
    graph.rollback(other)
    assert graph.counts(throwaway_batch)["nodes_in_batch"] == kept


def test_sweep_removes_what_this_batch_did_not_reassert(graph, small_tree, small_refs,
                                                        throwaway_batch, part_id):
    """A rerun converges on state: the provision the new run no longer asserts
    goes, rather than lingering as an orphan the graph still believes in."""
    first, second = f"{throwaway_batch}-1", f"{throwaway_batch}-2"
    graph.also_rollback(first)
    graph.also_rollback(second)

    load(graph, small_tree, small_refs, first)
    doomed = small_tree.children[0].children[1]           # <part>/9/9.2
    assert graph.read("MATCH (n:Node {id: $id}) RETURN count(n) AS n",
                      id=doomed.id)[0]["n"] == 1

    # the second run of the same part no longer contains 9.2, nor the ref to it
    small_tree.children[0].children = [small_tree.children[0].children[0]]
    load(graph, small_tree, [small_refs[0]], second)
    result = graph.sweep([part_id], second)

    assert graph.read("MATCH (n:Node {id: $id}) RETURN count(n) AS n",
                      id=doomed.id)[0]["n"] == 0, "the stale provision survived"
    assert result["nodes_deleted"] >= 1
    kept = small_tree.children[0].children[0]
    assert graph.read("MATCH (n:Node {id: $id}) RETURN n.batch_id AS b",
                      id=kept.id)[0]["b"] == second


def test_sweep_never_reaches_outside_its_scope(graph, small_tree, small_refs,
                                               throwaway_batch, part_id):
    first, second = f"{throwaway_batch}-a", f"{throwaway_batch}-b"
    graph.also_rollback(first)
    graph.also_rollback(second)
    load(graph, small_tree, small_refs, first)
    graph.run("CREATE (n:Node {id: $id, path: $path, batch_id: $b})",
              id=f"{second}-outside", path=f"{part_id}-other/1", b=first)
    graph.sweep([part_id], second)
    assert graph.read("MATCH (n:Node {id: $id}) RETURN count(n) AS n",
                      id=f"{second}-outside")[0]["n"] == 1, \
        "a sweep scoped to one part deleted a node in another part"


def test_salience_is_breadth_times_log_damped_frequency(graph, small_tree, small_refs,
                                                        small_vocab, throwaway_batch):
    sites, uses = small_vocab
    load(graph, small_tree, small_refs, throwaway_batch, sites, uses)
    nodes = list(walk(small_tree))
    scores = salience_mod.compute(nodes, small_refs, uses)
    applied = graph.apply_salience(scores.values, scores.term_values, scores.flagged)
    assert applied["nodes_updated"] == len(scores.values)

    target = small_tree.children[0].children[1]           # cited once, from one part
    assert scores.values[target.id] == pytest.approx(1 * math.log(2))
    stored = graph.read("MATCH (n:Node {id: $id}) RETURN n.salience AS s", id=target.id)
    assert stored[0]["s"] == pytest.approx(scores.values[target.id])
    term = graph.read("MATCH (t:Term {name: 'Buyer'}) RETURN t.salience AS s")
    assert term[0]["s"] == pytest.approx(scores.term_values["Buyer"])


def test_the_loader_counts_reconcile_with_what_the_database_holds(graph, small_tree,
                                                                  small_refs,
                                                                  throwaway_batch):
    rows = load(graph, small_tree, small_refs, throwaway_batch)
    counted = graph.counts(throwaway_batch)
    assert counted["nodes_in_batch"] == len(rows.nodes)
    assert counted["relationships_in_batch"] == len(rows.edges)


def test_the_batch_that_created_a_node_survives_a_later_reassertion(graph, small_tree,
                                                                    small_refs,
                                                                    throwaway_batch):
    """`batch_id` is the last batch to assert a row, which is what the sweep
    needs. `first_batch_id` is the batch that introduced it, so "created" and
    "confirmed" stay distinguishable."""
    first, second = f"{throwaway_batch}-c1", f"{throwaway_batch}-c2"
    graph.also_rollback(first)
    graph.also_rollback(second)
    load(graph, small_tree, small_refs, first)
    load(graph, small_tree, small_refs, second)
    row = graph.read("MATCH (n:Node {id: $id}) "
                     "RETURN n.batch_id AS last, n.first_batch_id AS first",
                     id=small_tree.id)[0]
    assert row["last"] == second
    assert row["first"] == first


def test_the_test_part_id_cannot_collide_with_a_real_part(part_id):
    """The guard on the hazard above: a sweep is a delete, and a test that
    swept a real part id would delete real data out of a shared database."""
    import config

    assert part_id.startswith("loadtest-")
    real_parts = {b["part"] for b in config.BATCHES.values()}
    assert part_id not in real_parts
    assert not any(part_id.startswith(p) or p.startswith(part_id)
                   for p in real_parts)


def test_a_sweep_of_one_part_leaves_a_real_looking_part_alone(graph, small_tree,
                                                              small_refs,
                                                              throwaway_batch, part_id):
    """Belt and braces on the same hazard, with a node whose path is exactly the
    real Core Terms part id."""
    guard_batch = f"{throwaway_batch}-guard"
    graph.also_rollback(guard_batch)
    load(graph, small_tree, small_refs, throwaway_batch)
    graph.run("CREATE (n:Node {id: $id, path: 'core-terms/99/99.9', batch_id: $b})",
              id=f"{guard_batch}-real-looking", b=guard_batch)
    graph.sweep([part_id], f"{throwaway_batch}-next")
    survived = graph.read("MATCH (n:Node {id: $id}) RETURN count(n) AS n",
                          id=f"{guard_batch}-real-looking")[0]["n"]
    assert survived == 1, "a sweep scoped to a test part reached real Core Terms paths"
