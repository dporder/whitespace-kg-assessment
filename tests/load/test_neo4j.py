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
from pipeline.load.rows import (legislation_rows, load_id_for, term_rows, tree_rows,
                                walk)


def build(tree, refs, batch, sites=None, uses=None, load_id=""):
    rows = tree_rows({tree.path: tree}, {tree.path: refs}, batch_id=batch,
                     document=None, load_id=load_id)
    rows.nodes.extend(legislation_rows({tree.path: refs}, [], batch_id=batch,
                                       load_id=load_id).nodes)
    if sites is not None or uses is not None:
        by_id = {n.id: n for n in walk(tree)}
        terms = term_rows(sites or [], uses or [], by_id, batch_id=batch,
                          load_id=load_id)
        rows.nodes.extend(terms.nodes)
        rows.edges.extend(terms.edges)
    return rows


def load(graph: Graph, tree, refs, batch, sites=None, uses=None):
    """Load exactly what this batch asserts, with a load id over those rows."""
    draft = build(tree, refs, batch, sites, uses)
    load_id = load_id_for(batch, draft)
    rows = build(tree, refs, batch, sites, uses, load_id=load_id)
    known = {r.key_value for r in rows.nodes}
    writable, _deferred = graph.partition_edges(rows.edges, known)
    graph.merge_nodes(rows.nodes)
    graph.merge_edges(writable, load_id=load_id)
    rows.load_id = load_id
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
    second_rows = load(graph, small_tree, [small_refs[0]], second)
    result = graph.sweep([part_id], second, load_id=second_rows.load_id)

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
    graph.sweep([part_id], second, load_id="a-load-id-nothing-here-carries")
    assert graph.read("MATCH (n:Node {id: $id}) RETURN count(n) AS n",
                      id=f"{second}-outside")[0]["n"] == 1, \
        "a sweep scoped to one part deleted a node in another part"


def test_salience_is_breadth_times_log_damped_frequency(graph, small_tree, small_refs,
                                                        small_vocab, throwaway_batch):
    sites, uses = small_vocab
    load(graph, small_tree, small_refs, throwaway_batch, sites, uses)
    applied = graph.recompute_salience(salience_mod.settings())
    assert applied["source"] == "graph"

    target = small_tree.children[0].children[1]           # cited once, from one part
    stored = graph.read("MATCH (n:Node {id: $id}) RETURN n.salience AS s", id=target.id)
    assert stored[0]["s"] == pytest.approx(1 * math.log(2))
    buyer = sites[0].term
    term = graph.read("MATCH (t:Term {name: $name}) RETURN t.salience AS s", name=buyer)
    assert term[0]["s"] == pytest.approx(1 * math.log(2))


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
    graph.sweep([part_id], f"{throwaway_batch}-next",
                load_id="a-load-id-nothing-here-carries")
    survived = graph.read("MATCH (n:Node {id: $id}) RETURN count(n) AS n",
                          id=f"{guard_batch}-real-looking")[0]["n"]
    assert survived == 1, "a sweep scoped to a test part reached real Core Terms paths"


def test_rollback_keeps_a_referent_another_batch_still_points_at(graph, small_tree,
                                                                small_refs,
                                                                throwaway_batch,
                                                                statute_key):
    """Term.name, Legislation.key and Concept.id are global keys, so two batches
    citing one statute meet at one node. Rolling back the second batch must not
    delete a node the first batch still points at, because DETACH DELETE would
    take the first batch's RESOLVES_TO with it and silently unresolve a ref that
    had a target. Found on the real graph: the load tests cited the Bribery Act
    and their rollback deleted the real document's Legislation node."""
    keeper, goer = f"{throwaway_batch}-keep", f"{throwaway_batch}-go"
    graph.also_rollback(keeper)
    graph.also_rollback(goer)

    load(graph, small_tree, small_refs, keeper)
    # a second batch cites the same statute from a node of its own
    graph.run("CREATE (n:Node {id: $id, path: $path, kind: 'ref', batch_id: $b})",
              id=f"{goer}-ref", path=f"{goer}-part/1/ref@0-5", b=goer)
    graph.run("MATCH (r:Node {id: $id}), (l:Legislation {key: $key}) "
              "MERGE (r)-[e:RESOLVES_TO]->(l) SET e.batch_id = $b",
              id=f"{goer}-ref", key=statute_key, b=goer)
    graph.run("MATCH (l:Legislation {key: $key}) SET l.batch_id = $b",
              key=statute_key, b=goer)

    result = graph.rollback(goer)

    assert statute_key in result["referents_kept"]
    survived = graph.read("MATCH (l:Legislation {key: $key}) RETURN count(l) AS n",
                          key=statute_key)[0]["n"]
    assert survived == 1, "a shared statute was deleted with someone else's batch"
    still_resolved = graph.read(
        "MATCH (r:Node)-[:RESOLVES_TO]->(l:Legislation {key: $key}) "
        "WHERE r.batch_id = $b RETURN count(r) AS n", key=statute_key, b=keeper)[0]["n"]
    assert still_resolved == 1, "the keeping batch lost its resolved edge"


def test_rollback_still_removes_a_referent_nobody_else_uses(graph, small_tree,
                                                            small_refs,
                                                            throwaway_batch,
                                                            statute_key):
    load(graph, small_tree, small_refs, throwaway_batch)
    result = graph.rollback(throwaway_batch)
    assert result["referents_kept"] == []
    assert graph.read("MATCH (l:Legislation {key: $key}) RETURN count(l) AS n",
                      key=statute_key)[0]["n"] == 0


# --------------------------------------------------------------------------
# the reviewer's probes, kept as regressions
# --------------------------------------------------------------------------
def test_four_edges_submitted_one_written_is_not_reported_as_four(graph, small_tree,
                                                                  small_refs,
                                                                  throwaway_batch):
    """The reviewer's probe. merge_edges used to return len(payload), so edges
    whose endpoint MATCH found nothing were counted as merged. Now the count
    comes from the database and the unwritable ones are partitioned out first."""
    from pipeline.schemas import GraphEdge

    rows = load(graph, small_tree, small_refs, throwaway_batch)
    real = small_tree.children[0].children[1].id
    edges = [GraphEdge(type="CONTAINS", src=real, dst=real, props={},
                       batch_id=throwaway_batch)]
    for missing in ("no-such-node-1", "no-such-node-2", "no-such-node-3"):
        edges.append(GraphEdge(type="CONTAINS", src=real, dst=missing, props={},
                               batch_id=throwaway_batch))

    known = {r.key_value for r in rows.nodes}
    writable, deferred = graph.partition_edges(edges, known)
    assert len(writable) == 1 and len(deferred) == 3
    result = graph.merge_edges(writable, load_id=rows.load_id)
    assert result == {"submitted": 1, "written": 1}

    # and submitting them all anyway still reports only what landed
    everything = graph.merge_edges(edges, load_id=rows.load_id)
    assert everything["submitted"] == 4
    assert everything["written"] == 1, "the database wrote one; four was the old lie"


def test_a_referent_another_batch_will_need_survives_an_unrelated_sweep(
        graph, small_tree, small_refs, throwaway_batch, part_id):
    """The reviewer's scenario. A Concept loaded for B2 whose member provisions
    live in B4 is legitimately edgeless until B4 arrives. The orphan cleanup
    used to be a global unscoped DELETE, so an unrelated sweep took it."""
    other = f"{throwaway_batch}-other"
    graph.also_rollback(other)
    concept_id = f"concept-waiting-{part_id}"
    graph.run("CREATE (c:Concept {id: $id, batch_id: $b, load_id: $l})",
              id=concept_id, b=other, l=f"{other}-load")

    rows = load(graph, small_tree, small_refs, throwaway_batch)
    referents = [r.key_value for r in rows.nodes
                 if r.key_field in ("name", "key") or "Concept" in r.labels]
    result = graph.sweep([part_id], throwaway_batch, load_id=rows.load_id,
                         referent_keys=referents)

    survived = graph.read("MATCH (c:Concept {id: $id}) RETURN count(c) AS n",
                          id=concept_id)[0]["n"]
    assert survived == 1, "an unrelated sweep deleted a concept waiting for its batch"
    assert concept_id not in result["orphan_referents"]


def test_a_sweep_only_cleans_up_referents_this_load_asserted(graph, small_tree,
                                                             small_refs,
                                                             throwaway_batch, part_id,
                                                             statute_key):
    """The load's own edgeless referent is still cleaned up."""
    rows = load(graph, small_tree, small_refs, throwaway_batch)
    graph.run("MATCH (:Node)-[r:RESOLVES_TO]->(:Legislation {key: $k}) DELETE r",
              k=statute_key)
    referents = [r.key_value for r in rows.nodes
                 if r.key_field in ("name", "key") or "Concept" in r.labels]
    result = graph.sweep([part_id], throwaway_batch, load_id=rows.load_id,
                         referent_keys=referents)
    assert statute_key in result["orphan_referents"]
    assert graph.read("MATCH (l:Legislation {key: $k}) RETURN count(l) AS n",
                      k=statute_key)[0]["n"] == 0


def test_a_rerun_of_the_same_batch_converges(graph, small_tree, small_refs,
                                             throwaway_batch, part_id):
    """The tester's defect. Keying the sweep on batch identity cannot see a
    rerun of the same batch, so a clause the rerun no longer asserts sits there
    wearing the same tag as everything else. The sweep keys on load_id."""
    load(graph, small_tree, small_refs, throwaway_batch)
    doomed = small_tree.children[0].children[1]
    assert graph.read("MATCH (n:Node {id: $id}) RETURN count(n) AS n",
                      id=doomed.id)[0]["n"] == 1

    # the same batch id, re-ingested without that clause and its refs
    small_tree.children[0].children = [small_tree.children[0].children[0]]
    rows = load(graph, small_tree, [small_refs[0]], throwaway_batch)
    result = graph.sweep([part_id], throwaway_batch, load_id=rows.load_id)

    assert graph.read("MATCH (n:Node {id: $id}) RETURN count(n) AS n",
                      id=doomed.id)[0]["n"] == 0, "a same-batch rerun did not converge"
    assert result["nodes_deleted"] >= 1


def test_an_identical_rerun_sweeps_nothing(graph, small_tree, small_refs,
                                           throwaway_batch, part_id):
    """load_id is a content hash, so an unchanged rerun computes the same id and
    the sweep finds nothing stale."""
    first = load(graph, small_tree, small_refs, throwaway_batch)
    second = load(graph, small_tree, small_refs, throwaway_batch)
    assert first.load_id == second.load_id
    result = graph.sweep([part_id], throwaway_batch, load_id=second.load_id)
    assert result["nodes_deleted"] == 0
    assert result["relationships_deleted"] == 0


def test_salience_from_the_graph_covers_batches_loaded_earlier(graph, small_tree,
                                                               small_refs,
                                                               small_vocab,
                                                               throwaway_batch,
                                                               part_id):
    """The point of recomputing from the graph: a provision gains breadth when a
    later batch citing it arrives, which a per-batch computation cannot see."""
    import math

    from pipeline.load import salience as salience_mod
    from pipeline.schemas import lineage_key, node_id

    sites, uses = small_vocab
    load(graph, small_tree, small_refs, throwaway_batch, sites, uses)
    target = small_tree.children[0].children[1]
    graph.recompute_salience(salience_mod.settings())
    before = graph.read("MATCH (n:Node {id: $id}) RETURN n.salience AS s",
                        id=target.id)[0]["s"]
    assert before == pytest.approx(1 * math.log(2))

    # a second part, loaded later, cites the same provision
    other = f"{throwaway_batch}-second"
    graph.also_rollback(other)
    other_part = f"{part_id}-two"
    citing = f"{other_part}/1"
    ref_path = f"{citing}/ref@0-5"
    graph.run("CREATE (p:Node {id: $pid, path: $ppath, kind: 'clause', batch_id: $b}) "
              "CREATE (r:Node {id: $rid, path: $rpath, kind: 'ref', batch_id: $b}) "
              "CREATE (p)-[:CONTAINS {batch_id: $b}]->(r)",
              pid=f"{other}-p", ppath=citing, rid=f"{other}-r", rpath=ref_path, b=other)
    graph.run("MATCH (r:Node {id: $rid}), (t:Node {id: $tid}) "
              "CREATE (r)-[:RESOLVES_TO {batch_id: $b}]->(t)",
              rid=f"{other}-r", tid=target.id, b=other)

    graph.recompute_salience(salience_mod.settings())
    after = graph.read("MATCH (n:Node {id: $id}) RETURN n.salience AS s",
                       id=target.id)[0]["s"]
    assert after == pytest.approx(2 * math.log(3)), "breadth did not grow with the batch"
    assert after > before
