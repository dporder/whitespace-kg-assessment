"""Resolution: the document's own scope rules, and what it refuses to do.

JS1 1.3.8 (Clauses and Schedules mean the Core Terms'; parts, paragraphs,
annexes and tables in a Schedule mean that Schedule's), 1.3.9 (Paragraphs mean
the appropriate Schedule's), the title parenthetical, the mislabelled
"Clause 1.x inside a Schedule" case, and the rule that outranks all of them:
a ref never mints its target.
"""
from __future__ import annotations

import pytest

from pipeline.references.build import infer_identity, ref_node, span_intact
from pipeline.references.corpus import Corpus
from pipeline.references.detect import detect_part
from pipeline.references.resolve import resolve_pointer


@pytest.fixture
def corpus(core_terms, joint_schedule_1) -> Corpus:
    return Corpus.from_trees({"core-terms": core_terms,
                              "joint-schedule-1": joint_schedule_1})


def resolved_refs(part_id, tree, corpus, identity) -> dict:
    """Every ref in one part, keyed by the pointing words."""
    found = detect_part(part_id, tree)
    out = {}
    for order, pointer in enumerate(found.pointers):
        parent = corpus.node(pointer.parent_path)
        resolution = resolve_pointer(corpus, pointer)
        ref = ref_node(pointer, resolution, parent, identity, order=order, batch_id="B1")
        assert span_intact(ref, parent), ref.path
        out.setdefault(ref.text, []).append(ref)
    return out


def test_a_clause_in_core_terms_resolves_by_js1_1_3_8(core_terms, corpus, identity):
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Clause 3.1"][0]
    assert (ref.status, ref.scope_rule, ref.resolver) == ("resolved", "js1_1.3.8", "scope")
    assert ref.target_path == "core-terms/3/3.1"
    assert ref.confidence is None, "a deterministic resolver never asserts a confidence"


def test_the_mislabelled_clause_inside_a_schedule_is_ambiguous_with_both_candidates(
        joint_schedule_1, corpus, identity):
    """SPEC 4: the pack writes "Clause 1.x" inside Schedules three times. Those
    resolve to the stipulated target, carry status ambiguous with both
    candidates and the reason, and go to review. Never silently, never dropped."""
    ref = resolved_refs("joint-schedule-1", joint_schedule_1, corpus, identity)["Clause 1.2"][0]
    assert ref.status == "ambiguous"
    assert ref.target_path == "core-terms/1/1.2", "the stipulated reading is still stamped"
    assert {c.path for c in ref.candidates} == {"core-terms/1/1.2", "joint-schedule-1/1/1.2"}
    stipulated = next(c for c in ref.candidates if c.path.startswith("core-terms"))
    local = next(c for c in ref.candidates if c.path.startswith("joint-schedule-1"))
    assert stipulated.score > local.score
    assert any(a.startswith("mislabelled_cross_reference") for a in ref.anomalies)


def test_a_clause_in_a_schedule_with_no_local_twin_is_simply_resolved(
        joint_schedule_1, corpus, identity):
    joint_schedule_1.children[0].children[0].text = "Clause 3.1.2 applies here."
    ref = resolved_refs("joint-schedule-1", joint_schedule_1, corpus, identity)["Clause 3.1.2"][0]
    assert (ref.status, ref.target_path) == ("resolved", "core-terms/3/3.1/3.1.2")


def test_a_paragraph_inside_a_schedule_resolves_by_js1_1_3_9(joint_schedule_1, corpus, identity):
    ref = resolved_refs("joint-schedule-1", joint_schedule_1, corpus, identity)["paragraph 2.1"][0]
    assert (ref.status, ref.scope_rule) == ("resolved", "js1_1.3.9")
    assert ref.target_path == "joint-schedule-1/2/2.1"


def test_an_annex_inside_a_schedule_resolves_to_that_schedules_annex(
        joint_schedule_1, corpus, identity):
    ref = resolved_refs("joint-schedule-1", joint_schedule_1, corpus, identity)["Annex 1"][0]
    assert (ref.status, ref.scope_rule) == ("resolved", "js1_1.3.8")
    assert ref.target_path == "joint-schedule-1/Annex 1"


def test_a_paragraph_cited_from_core_terms_names_no_schedule_so_stays_ambiguous(
        core_terms, corpus, identity):
    """"paragraph 2.1 of Joint Schedule 1" names its schedule; a bare one would
    not, and JS1 1.3.9 has nothing to point at."""
    core_terms.children[2].text = "See paragraph 2.1 for detail."   # drops the tail
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["paragraph 2.1"][0]
    assert ref.status == "ambiguous"
    assert ref.target_path is None
    assert any("names none" in a for a in ref.anomalies)


def test_a_named_scope_tail_sends_a_paragraph_to_its_schedule(core_terms, corpus, identity):
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["paragraph 2.1"][0]
    assert (ref.status, ref.target_path) == ("resolved", "joint-schedule-1/2/2.1")


def test_a_bare_schedule_number_is_ambiguous_across_the_three_families(core_terms, corpus, identity):
    core_terms.children[3].text = "See Schedule 2 for detail."
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Schedule 2"][0]
    assert ref.status == "ambiguous"
    assert {c.path for c in ref.candidates} == {"framework-schedule-2", "joint-schedule-2",
                                                "call-off-schedule-2"}
    assert ref.target_path is None


def test_a_citation_to_a_part_that_has_not_arrived_stays_unresolved(core_terms, corpus, identity):
    """Refs never mint target nodes: the conventional id is kept as a candidate
    string and the ref waits for the part to land."""
    core_terms.children[3].text = "See Framework Schedule 4 (Framework Management)."
    ref = resolved_refs("core-terms", core_terms, corpus, identity)[
        "Framework Schedule 4 (Framework Management)"][0]
    assert ref.status == "unresolved"
    assert ref.target_path is None
    assert [c.path for c in ref.candidates] == ["framework-schedule-4"]
    assert not corpus.exists("framework-schedule-4")


def test_a_title_parenthetical_naming_an_ingested_part_resolves_by_title(
        core_terms, corpus, identity):
    core_terms.children[3].text = "See Schedule 1 (Definitions) for detail."
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Schedule 1 (Definitions)"][0]
    assert (ref.status, ref.scope_rule) == ("resolved", "title_paren")
    assert ref.target_path == "joint-schedule-1"


def test_a_title_parenthetical_can_name_a_part_the_register_knows_of(core_terms, corpus, identity):
    """Once stage 0's part map exists, a title names a part this batch lacks."""
    corpus.register_parts({"call-off-schedule-6": "Call-Off Schedule 6 (Materials)"})
    core_terms.children[3].text = "See Schedule 6 (Materials) for detail."
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Schedule 6 (Materials)"][0]
    assert (ref.status, ref.scope_rule) == ("unresolved", "title_paren")
    assert [c.path for c in ref.candidates] == ["call-off-schedule-6"]


def test_a_citation_to_a_number_that_does_not_exist_offers_its_ancestor(
        core_terms, corpus, identity):
    core_terms.children[3].text = "See Clause 3.1.9 for detail."
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Clause 3.1.9"][0]
    assert ref.status == "unresolved"
    assert [c.path for c in ref.candidates] == ["core-terms/3/3.1"]
    assert "nearest enclosing provision" in ref.candidates[0].reason


def test_anaphora_is_never_resolved_by_the_grammar(core_terms, corpus, identity):
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["this Clause"][0]
    assert ref.status == "ambiguous"
    assert ref.target_path is None
    assert ref.resolver == "scope"
    assert any("anaphoric reference" in a for a in ref.anomalies)


def test_legislation_is_external_and_resolved_by_the_grammar(core_terms, corpus, identity):
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Bribery Act 2010"][0]
    assert (ref.status, ref.resolver, ref.scope_rule) == ("external", "grammar", "none")
    assert ref.target_path == "legislation/bribery-act-2010"


def test_a_range_mints_one_ref_per_member_with_distinct_ids(core_terms, corpus, identity):
    refs = [r for group in resolved_refs("core-terms", core_terms, corpus, identity).values()
            for r in group if r.ref_kind == "clause" and r.group_id]
    members = {r.target_path or r.text for r in refs}
    assert "core-terms/1" in members and "core-terms/4" in members
    ids = [r.id for r in refs]
    assert len(ids) == len(set(ids)), "range members collided on one id"
    paths = [r.path for r in refs if "+" in r.path]
    assert paths, "an interior range member should carry the disambiguating suffix"


def test_every_member_of_a_list_shares_one_group_id(core_terms, corpus, identity):
    refs = [r for group in resolved_refs("core-terms", core_terms, corpus, identity).values()
            for r in group if r.group_id]
    groups = {r.group_id for r in refs}
    assert len(groups) >= 1
    for group in groups:
        assert len([r for r in refs if r.group_id == group]) > 1


def test_identity_is_derived_from_the_trees_own_ids(core_terms, joint_schedule_1,
                                                    doc_id, version):
    derived = infer_identity([core_terms, joint_schedule_1])
    assert derived.verified is False, "the test document id is not a config candidate"
    derived = infer_identity([core_terms, joint_schedule_1], document=doc_id)
    assert (derived.document, derived.version, derived.verified) == (doc_id, version, True)
