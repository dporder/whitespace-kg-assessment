"""Detection: the citation grammar, ranges, anaphora, the orphan scan.

Scored separately from resolution (SPEC 2.2), so tested separately from it.
"""
from __future__ import annotations

from pipeline.references import grammar, legislation
from pipeline.references.detect import detect_part


def spans(text, citations):
    return [(text[c.span[0]:c.span[1]], c.ref_kind) for c in citations]


def test_a_lone_citation_owns_the_whole_phrase():
    text = "Subject to Clause 3.1.2 and Framework Schedule 4 (Framework Management), each"
    found = grammar.find_citations(text)
    assert spans(text, found) == [("Clause 3.1.2", "clause"),
                                  ("Framework Schedule 4 (Framework Management)", "schedule")]
    assert found[1].members[0].title_paren == "Framework Management"


def test_a_list_anchors_each_member_to_its_own_number():
    text = "subject to Clauses 3.1.1 and 3.1.2, Schedule 2 and the rest."
    citation = grammar.find_citations(text)[0]
    assert [text[m.span[0]:m.span[1]] for m in citation.members] == ["3.1.1", "3.1.2"]
    # the list stops where the numbers stop: "Schedule 2" is its own citation
    assert spans(text, grammar.find_citations(text))[1] == ("Schedule 2", "schedule")


def test_the_committed_fixture_spans_are_reproduced_exactly():
    """The orchestrator hand-anchored these offsets; the grammar must agree."""
    text = ("Any New IPR created under a Contract is owned by the Central Buying Office "
            "subject to Clauses 3.1.1 and 3.1.2, Schedule 2 and the Bribery Act 2010.")
    hits = legislation.find_legislation(text)
    citations = grammar.find_citations(text, consumed=[h.span for h in hits])
    members = [m.span for c in citations for m in c.members]
    assert (94, 99) in members and (104, 109) in members
    assert any(c.span == (111, 121) for c in citations)
    assert hits[0].span == (130, 146)


def test_a_range_expands_inclusively_and_the_interior_owns_no_characters():
    text = "See Clauses 3 to 6 for detail."
    citation = grammar.find_citations(text)[0]
    assert [m.number for m in citation.members] == ["3", "4", "5", "6"]
    interior = [m for m in citation.members if m.expanded]
    assert [m.number for m in interior] == ["4", "5"]
    assert all(text[m.span[0]:m.span[1]] == "3 to 6" for m in interior)
    assert [m.expansion_index for m in interior] == [1, 2]


def test_a_range_with_no_shared_prefix_is_recorded_not_guessed():
    citation = grammar.find_citations("See Clauses 10.4.3 to 11.2 for detail.")[0]
    assert [m.number for m in citation.members] == ["10.4.3", "11.2"]
    assert any("range_not_expanded" in n for n in citation.notes)


def test_a_range_beyond_the_limit_is_recorded_not_expanded():
    citation = grammar.find_citations("See Clauses 1 to 99.", max_range=10)[0]
    assert [m.number for m in citation.members] == ["1", "99"]
    assert any("exceeds the expansion limit" in n for n in citation.notes)


def test_lowercase_paragraph_is_detected():
    """The pack writes "paragraph N of this Schedule" 27 times, lowercase."""
    text = "as set out in paragraph 5 of this Schedule."
    citation = grammar.find_citations(text)[0]
    assert citation.ref_kind == "paragraph"
    assert citation.members[0].number == "5"
    assert citation.context["anaphoric"] is True


def test_a_scope_tail_does_not_also_surface_as_anaphora():
    text = "as set out in paragraph 5 of this Schedule."
    citations = grammar.find_citations(text)
    consumed = [c.span for c in citations]
    consumed += [c.context["span"] for c in citations if c.context and c.context["anaphoric"]]
    assert grammar.find_anaphora(text, consumed) == []


def test_a_named_scope_tail_is_still_its_own_citation():
    """"paragraph 5 of Joint Schedule 1" cites the schedule too, which is what
    an impact query over a schedule needs to see."""
    text = "See paragraphs 1.3.8 and 1.3.9 of Joint Schedule 1."
    kinds = [c.ref_kind for c in grammar.find_citations(text)]
    assert kinds == ["paragraph", "schedule"]


def test_anaphora_is_detected_by_pattern():
    text = "Nothing in this Clause limits that Schedule."
    found = grammar.find_anaphora(text, [])
    assert [(f.surface, f.ref_kind) for f in found] == [("this Clause", "clause"),
                                                        ("that Schedule", "schedule")]


def test_orphan_keywords_are_triaged_not_dropped(core_terms):
    """Table is a unit the interpretation clause names and the spec's citation
    grammar does not, so it must surface for triage rather than vanish."""
    found = detect_part("core-terms", core_terms)
    orphans = {(o["keyword"], o["verdict"]) for o in found.orphans}
    assert ("Table", "possible_missed_citation") in orphans


def test_generic_prose_use_is_not_a_missed_citation():
    orphans = grammar.find_orphans("The Supplier shall play its part and act fairly.", [])
    assert {o["verdict"] for o in orphans} == {"generic_prose"}


def test_a_citation_inside_a_title_is_recorded_not_minted(core_terms):
    """A ref anchors to its parent's text; a title has none, and schemas.Node
    forbids ref children on a node with no text."""
    core_terms.children[0].title = "Definitions under Clause 2.1"
    found = detect_part("core-terms", core_terms)
    assert len(found.title_citations) == 1
    assert found.title_citations[0]["surface"] == "Clause 2.1"
    assert all(p.parent_path != "core-terms/1" for p in found.pointers)


def test_detection_counts_are_reported_separately(core_terms):
    counts = detect_part("core-terms", core_terms).counts()
    assert counts["pointers"] > 0
    assert set(counts) >= {"pointers", "by_ref_kind", "by_method", "orphan_keywords",
                           "orphans_by_verdict", "anaphora", "range_expanded"}
