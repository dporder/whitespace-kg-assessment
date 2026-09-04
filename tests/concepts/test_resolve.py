"""Resolution: term collisions first, then near-duplicate merging."""
from __future__ import annotations

import config
from pipeline.concepts import resolve as resolve_mod
from pipeline.concepts.scan import ProposedConcept


def proposal(label, confidence=0.8, members=("n1",), scope="clauses/1",
             relations=None) -> ProposedConcept:
    return ProposedConcept(label=label, confidence=confidence,
                           member_node_ids=list(members),
                           member_paths=[f"path/{m}" for m in members],
                           relations=relations or [], scope_path=scope,
                           part="clauses", unit_kind="heading")


class StubVectors:
    """A vector per label, so cosine is exercised without a provider."""

    def __init__(self, table):
        self.table = table
        self.available = True
        self.note = ""

    def warm(self, texts):
        return None

    def get(self, text):
        return self.table.get(text)

    def best_match(self, text, candidates):
        from pipeline.embeddings.client import cosine
        vector = self.get(text)
        if vector is None:
            return None
        pool = [(c, self.get(c)) for c in candidates if self.get(c) is not None]
        if not pool:
            return None
        return max(((c, cosine(vector, v)) for c, v in pool),
                   key=lambda cs: (cs[1], cs[0]))


def resolve(proposed, trees, surfaces=None, vectors=None, threshold=None):
    if vectors is not None:
        original = resolve_mod.Vectors
        resolve_mod.Vectors = lambda _embedder: vectors
        try:
            return resolve_mod.resolve(proposed, trees, None, threshold, surfaces)
        finally:
            resolve_mod.Vectors = original
    return resolve_mod.resolve(proposed, trees, None, threshold, surfaces)


# --------------------------------------------------------- declared terms


def test_the_declared_vocabulary_is_derived_from_the_trees_not_from_stage_4(
        two_part_trees):
    """SPEC 3 keeps stages 3 to 6 from reading each other's output, and SPEC 2.4
    still needs the declared list here. Both hold because the list is re-derived
    from the trees by the same pure function stage 4 uses."""
    surfaces = resolve_mod.declared_surfaces(two_part_trees)
    assert surfaces["exit management"] == "Exit Management"
    assert surfaces["widget"] == "Widget"


def test_a_concept_whose_label_is_a_declared_term_is_never_minted(two_part_trees):
    result = resolve(
        [proposal("Exit Management"), proposal("termination triggers")],
        two_part_trees)
    assert [c.label for c in result.concepts] == ["termination triggers"]
    assert len(result.collisions) == 1
    collision = result.collisions[0]
    assert collision.term == "Exit Management"
    assert "outranks" in collision.as_dict()["ruling"]


def test_an_alias_collision_also_blocks_the_concept(two_part_trees):
    surfaces = resolve_mod.declared_surfaces(two_part_trees)
    surfaces["cbo"] = "Central Buying Office"
    result = resolve([proposal("CBO")], two_part_trees, surfaces=surfaces)
    assert result.concepts == []
    assert result.collisions[0].term == "Central Buying Office"


def test_an_embedding_near_duplicate_of_a_term_blocks_the_concept(two_part_trees):
    vectors = StubVectors({"exit management": [1.0, 0.0, 0.0, 0.0],
                           "widget": [0.0, 1.0, 0.0, 0.0],
                           "exit management activities": [0.995, 0.1, 0.0, 0.0]})
    result = resolve([proposal("exit management activities")], two_part_trees,
                     vectors=vectors)
    assert result.concepts == []
    assert result.collisions[0].how == "embedding_near_duplicate"
    assert result.collisions[0].score >= config.CONCEPT_MERGE_COSINE


def test_a_concept_below_the_threshold_survives(two_part_trees):
    vectors = StubVectors({"exit management": [1.0, 0.0, 0.0, 0.0],
                           "widget": [0.0, 1.0, 0.0, 0.0],
                           "termination triggers": [0.0, 0.0, 1.0, 0.0]})
    result = resolve([proposal("termination triggers")], two_part_trees,
                     vectors=vectors)
    assert [c.label for c in result.concepts] == ["termination triggers"]
    assert result.collisions == []


# ------------------------------------------------------- near-duplicate merge


def test_near_duplicates_merge_and_the_log_records_the_score(two_part_trees):
    vectors = StubVectors({"exit management": [1.0, 0.0, 0.0, 0.0],
                           "widget": [0.0, 1.0, 0.0, 0.0],
                           "termination triggers": [0.0, 0.0, 1.0, 0.0],
                           "triggers for termination": [0.0, 0.0, 0.99, 0.14]})
    result = resolve([proposal("termination triggers", 0.9, ["n1"]),
                      proposal("triggers for termination", 0.6, ["n2"])],
                     two_part_trees, vectors=vectors)
    assert len(result.concepts) == 1
    merged = result.concepts[0]
    assert merged.label == "termination triggers", "the more confident label wins"
    assert set(merged.member_node_ids) == {"n1", "n2"}
    assert len(result.merges) == 1
    entry = result.merges[0].as_dict()
    assert entry["kept"] == "termination triggers"
    assert entry["absorbed"] == "triggers for termination"
    assert entry["method"] == resolve_mod.COSINE
    assert entry["score"] >= config.CONCEPT_MERGE_COSINE


def test_a_chain_of_duplicates_collapses_to_one_cluster(two_part_trees):
    # A true transitive chain: a~b and b~c are above the threshold, a~c is not,
    # which is what union-find is for and what pairwise merging alone would miss.
    vectors = StubVectors({"exit management": [0.0, 0.0, 1.0, 0.0],
                           "widget": [0.0, 0.0, 0.0, 1.0],
                           "a": [1.0, 0.0, 0.0, 0.0],
                           "b": [0.85, 0.5268, 0.0, 0.0],
                           "c": [0.45, 0.893, 0.0, 0.0]})
    result = resolve([proposal("a", 0.9, ["n1"]), proposal("b", 0.8, ["n2"]),
                      proposal("c", 0.7, ["n3"])], two_part_trees, vectors=vectors)
    assert len(result.concepts) == 1
    assert set(result.concepts[0].member_node_ids) == {"n1", "n2", "n3"}


def test_distinct_concepts_are_not_merged(two_part_trees):
    vectors = StubVectors({"exit management": [1.0, 0.0, 0.0, 0.0],
                           "widget": [0.0, 1.0, 0.0, 0.0],
                           "termination triggers": [0.0, 0.0, 1.0, 0.0],
                           "payment mechanics": [0.0, 0.0, 0.0, 1.0]})
    result = resolve([proposal("termination triggers"), proposal("payment mechanics")],
                     two_part_trees, vectors=vectors)
    assert len(result.concepts) == 2
    assert result.merges == []


def test_the_merged_concept_takes_the_highest_altitude_scope(two_part_trees):
    vectors = StubVectors({"exit management": [1.0, 0.0, 0.0, 0.0],
                           "widget": [0.0, 1.0, 0.0, 0.0],
                           "termination triggers": [0.0, 0.0, 1.0, 0.0],
                           "triggers for termination": [0.0, 0.0, 0.999, 0.02]})
    result = resolve([proposal("termination triggers", 0.9, ["n1"], scope="clauses/1"),
                      proposal("triggers for termination", 0.8, ["n2"],
                               scope="clauses")],
                     two_part_trees, vectors=vectors)
    assert result.concepts[0].scope_path == "clauses"


# ------------------------------------------------------ the lexical fallback


def test_without_vectors_a_weaker_check_runs_and_says_so(two_part_trees):
    """A lexical check that called itself a cosine check would be a lie about
    what was measured."""
    result = resolve([proposal("termination triggers", 0.9, ["n1"]),
                      proposal("supplier termination triggers", 0.5, ["n2"])],
                     two_part_trees)
    assert result.method == resolve_mod.LEXICAL
    assert "lexical" in result.note
    assert len(result.concepts) == 1
    assert result.merges[0].method == resolve_mod.LEXICAL


def test_the_lexical_fallback_does_not_merge_unrelated_labels(two_part_trees):
    result = resolve([proposal("termination triggers"), proposal("payment mechanics")],
                     two_part_trees)
    assert len(result.concepts) == 2


# ---------------------------------------------------------------- contract


def test_every_minted_concept_is_flagged_llm_derived_with_its_confidence(
        two_part_trees):
    result = resolve([proposal("termination triggers", 0.63)], two_part_trees)
    assert result.concepts[0].llm_derived is True
    assert result.concepts[0].confidence == 0.63


def test_relations_point_at_minted_concepts(two_part_trees):
    from pipeline.concepts.scan import concept_id
    result = resolve([
        proposal("termination triggers", 0.9, ["n1"],
                 relations=[{"relation": "depends_on", "to": "payment mechanics"}]),
        proposal("payment mechanics", 0.8, ["n2"])], two_part_trees)
    relation = next(r for c in result.concepts for r in c.relations)
    assert relation.label == "depends_on"
    assert relation.dst == concept_id("clauses/1", "payment mechanics")
    assert relation.dst in {c.id for c in result.concepts}


def test_a_relation_target_that_was_merged_away_is_remapped(two_part_trees):
    """The merged cluster's id is nobody's own id, so a relation into an
    absorbed concept has to follow it into the cluster or it dangles. This was
    a real defect: the first live run produced six dangling relations."""
    vectors = StubVectors({"exit management": [1.0, 0.0, 0.0, 0.0],
                           "widget": [0.0, 1.0, 0.0, 0.0],
                           "payment mechanics": [0.0, 0.0, 1.0, 0.0],
                           "payment mechanism": [0.0, 0.0, 0.999, 0.02],
                           "termination triggers": [0.0, 0.0, 0.0, 1.0]})
    result = resolve([
        proposal("payment mechanics", 0.9, ["n1"]),
        proposal("payment mechanism", 0.5, ["n2"]),
        proposal("termination triggers", 0.8, ["n3"],
                 relations=[{"relation": "depends_on", "to": "payment mechanism"}]),
    ], two_part_trees, vectors=vectors)
    minted = {c.id for c in result.concepts}
    assert len(minted) == 2
    relation = next(r for c in result.concepts for r in c.relations)
    assert relation.dst in minted
    assert result.dropped_relations == []


def test_a_relation_to_a_concept_that_was_never_minted_is_dropped_and_logged(
        two_part_trees):
    result = resolve([
        proposal("termination triggers", 0.9, ["n1"],
                 relations=[{"relation": "depends_on", "to": "a concept nobody "
                                                             "proposed"}])],
        two_part_trees)
    assert result.concepts[0].relations == []
    assert result.dropped_relations[0]["reason"] == "target is not a minted concept"


def test_a_relation_verb_that_is_really_a_concept_label_is_dropped(two_part_trees):
    """Models reach for the `label` key and put the source concept's own name in
    it. That is not a relation, and minting it would put a concept label where a
    verb phrase belongs on every CONCEPT_REL edge."""
    result = resolve([
        proposal("termination triggers", 0.9, ["n1"],
                 relations=[{"relation": "termination triggers",
                             "to": "payment mechanics"}]),
        proposal("payment mechanics", 0.8, ["n2"])], two_part_trees)
    assert all(not c.relations for c in result.concepts)
    assert "verb phrase" in result.dropped_relations[0]["reason"]


def test_resolution_is_deterministic(two_part_trees):
    proposals = [proposal("termination triggers", 0.9, ["n1"]),
                 proposal("payment mechanics", 0.8, ["n2"]),
                 proposal("exit planning", 0.7, ["n3"])]
    first = resolve(proposals, two_part_trees).as_dict()
    for _ in range(3):
        assert resolve(proposals, two_part_trees).as_dict() == first
