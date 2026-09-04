"""The three matching rules, aliases, scope and the headword exclusion."""
from __future__ import annotations

from pipeline.vocabulary import declared, discovery, matching, sites as sites_mod, treeio
from pipeline.vocabulary.sites import MergedSite, PartVocabulary, Surface

BATCHES = {"B2": {"part": "defs-schedule", "pages": (1, 2), "genre": "definitions"}}


def vocab(*pairs: tuple[str, str], collisions: dict[str, list[str]] | None = None,
          part: str = "p") -> PartVocabulary:
    """A vocabulary from (surface, term) pairs, for the rule-level tests."""
    collisions = collisions or {}
    return PartVocabulary(part=part, surfaces={
        surface: Surface(surface=surface, term=term, is_alias=surface != term,
                         definition_used="document",
                         collides_with=collisions.get(surface, []))
        for surface, term in pairs})


def spans(text: str, v: PartVocabulary) -> list[tuple[int, int, str]]:
    return [(s, e, sur.term) for s, e, sur in matching.select(matching.candidates(text, v))]


# ------------------------------------------------------------- the three rules


def test_matching_is_case_sensitive():
    v = vocab(("Default", "Default"))
    assert spans("A Default occurred.", v) == [(2, 9, "Default")]
    assert spans("the default settings", v) == []


def test_longest_match_wins_and_overlaps_are_forbidden():
    """`Call-Off Contract` beats `Contract`: the shorter term inside the longer
    match is a fragment of it, not an independent use."""
    v = vocab(("Contract", "Contract"), ("Call-Off Contract", "Call-Off Contract"))
    assert spans("Each Call-Off Contract is a Contract.", v) == [
        (5, 22, "Call-Off Contract"), (28, 36, "Contract")]


def test_word_boundaries_are_required():
    v = vocab(("Contract", "Contract"))
    assert spans("Contracts are plural.", v) == []
    assert spans("The Contract's term.", v) == [(4, 12, "Contract")]


def test_selection_is_deterministic_under_input_order():
    v = vocab(("Widget", "Widget"), ("Widget Register", "Widget Register"))
    text = "The Widget Register lists each Widget."
    first = spans(text, v)
    for _ in range(5):
        assert spans(text, v) == first


# ----------------------------------------------------------------- aliases


def test_an_alias_carries_the_canonical_term_with_the_alias_span(clauses_part):
    """SPEC 2.3: aliases match with the same rules as full forms, and the record
    carries the canonical term with the alias's span."""
    sites = declared.ingest_part(clauses_part, {})
    found = discovery.discover_part(clauses_part)
    merged, _u = sites_mod.merge(sites, found.sites, found.aliases, set())
    v = sites_mod.vocabulary_for("clauses", merged)
    matches = matching.match_part(clauses_part, v, merged, lambda _n: False,
                                  treeio.section_of(clauses_part))
    alias_hits = [m for m in matches if m.is_alias]
    assert alias_hits, "the HB alias should match like a full form"
    hit = alias_hits[0]
    assert hit.surface == "HB"
    assert hit.term in ("Handover Body", "Holding Body")
    node = {n.id: n for n in treeio.walk(clauses_part)}[hit.node_id]
    assert node.text[hit.span[0]:hit.span[1]] == "HB"


# ------------------------------------------------------------------- scope


def test_a_part_local_definition_shadows_the_document_level_one(
        document_definitions_part, clauses_part):
    """`Widget` is defined document-wide and again inside the call-off schedule.
    Inside that part the local definition governs, and `definition_used` says so."""
    merged = []
    for part, batches in ((document_definitions_part, BATCHES), (clauses_part, {})):
        d = declared.ingest_part(part, batches)
        f = discovery.discover_part(part)
        merged.extend(sites_mod.merge(d, f.sites, f.aliases, {"defs-schedule"})[0])

    doc_v = sites_mod.vocabulary_for("defs-schedule", merged)
    local_v = sites_mod.vocabulary_for("clauses", merged)
    assert doc_v.surfaces["Widget"].definition_used == "document"
    assert local_v.surfaces["Widget"].definition_used == "part:clauses"
    # A term with no local definition still comes from the document level.
    assert local_v.surfaces["Widget Register"].definition_used == "document"


def test_a_term_defined_only_in_another_part_is_not_matched_here(
        document_definitions_part, clauses_part):
    merged = []
    for part, batches in ((document_definitions_part, BATCHES), (clauses_part, {})):
        d = declared.ingest_part(part, batches)
        f = discovery.discover_part(part)
        merged.extend(sites_mod.merge(d, f.sites, f.aliases, {"defs-schedule"})[0])
    doc_v = sites_mod.vocabulary_for("defs-schedule", merged)
    assert "Handover Body" not in doc_v.surfaces
    assert "Handover Body" in doc_v.suppressed_out_of_scope


# --------------------------------------------------------------- headwords


def test_the_headword_of_a_definition_is_not_a_use_of_itself(
        document_definitions_part):
    sites = declared.ingest_part(document_definitions_part, BATCHES)
    found = discovery.discover_part(document_definitions_part)
    merged, _u = sites_mod.merge(sites, found.sites, found.aliases, {"defs-schedule"})
    v = sites_mod.vocabulary_for("defs-schedule", merged)
    matches = matching.match_part(document_definitions_part, v, merged,
                                  lambda _n: False,
                                  treeio.section_of(document_definitions_part))
    label_ids = {s.raw.term_node_id for s in merged}
    assert not [m for m in matches if m.node_id in label_ids]


def test_uses_inside_a_definition_text_are_kept(document_definitions_part):
    """They are the DEFINED_USING raw material stage 7 derives its edges from."""
    sites = declared.ingest_part(document_definitions_part, BATCHES)
    found = discovery.discover_part(document_definitions_part)
    merged, _u = sites_mod.merge(sites, found.sites, found.aliases, {"defs-schedule"})
    v = sites_mod.vocabulary_for("defs-schedule", merged)
    matches = matching.match_part(document_definitions_part, v, merged,
                                  lambda _n: False,
                                  treeio.section_of(document_definitions_part))
    definition_nodes = {s.raw.definition_node_id for s in merged}
    inside = [m for m in matches if m.node_id in definition_nodes]
    assert {m.term for m in inside} >= {"Widget"}


# ------------------------------------------------------------- inflection


def _site(term: str):
    from pipeline.vocabulary import sites as s
    return s.MergedSite(raw=declared.RawSite(
        term=term, definition_node_id="d", scope="document", part="p"),
        source="declared")


def test_inflected_surfaces_match_and_carry_the_canonical_term():
    """JS1 1.3.1: "the singular includes the plural and vice versa". The record
    carries the printed term with the inflected surface's span, exactly as an
    alias does."""
    from pipeline.vocabulary import sites as s
    from tests.vocabulary.conftest import mk
    node = mk("p/1/1.1", "clause", order=2, label="1.1",
              text="Each Working Day the Supplier reviews all Working Days.")
    part = mk("p", "part", order=0, title="Part", part_family="core",
              children=[node])
    site = _site("Working Day")
    vocab = s.vocabulary_for("p", [site])
    assert vocab.surfaces["Working Days"].is_inflected is True
    assert vocab.surfaces["Working Day"].is_inflected is False
    ms = matching.match_part(part, vocab, [site], lambda _n: False,
                             treeio.section_of(part))
    assert [m.term for m in ms] == ["Working Day", "Working Day"]
    inflected = [m for m in ms if m.is_inflected]
    assert len(inflected) == 1
    assert node.text[inflected[0].span[0]:inflected[0].span[1]] == "Working Days"


def test_the_inflection_rule_runs_both_ways():
    from pipeline.vocabulary.sites import inflections
    assert "Contracts" in inflections("Contract")
    assert "Parties" in inflections("Party")
    assert "Party" in inflections("Parties")
    assert "Contract" in inflections("Contracts")
    assert "Contract" not in inflections("Contract")


def test_longest_match_still_wins_over_the_expanded_surface_set():
    """`Call-Off Contracts` must beat the inflected `Contracts`, or the
    expansion would quietly break the rule it sits under."""
    from pipeline.vocabulary import sites as s
    sites_list = [_site("Contract"), _site("Call-Off Contract")]
    vocab = s.vocabulary_for("p", sites_list)
    got = spans("Each Call-Off Contracts clause binds the Contracts.", vocab)
    assert got[0][2] == "Call-Off Contract"
    assert got[0][:2] == (5, 23)
    assert got[1][2] == "Contract"


def test_a_printed_surface_beats_an_inflection_of_another_term():
    """The collision class the plural rule introduces: `Parties` is a defined
    term in its own right and also the plural of `Party`. The string the
    drafters printed governs, and the pair is routed as an alias collision so a
    checker decides rather than the matcher guessing."""
    from pipeline.vocabulary import sites as s
    from tests.vocabulary.conftest import mk
    sites_list = [_site("Party"), _site("Parties")]
    vocab = s.vocabulary_for("p", sites_list)
    assert vocab.surfaces["Parties"].term == "Parties", "the printed term governs"
    assert vocab.surfaces["Parties"].is_inflected is False
    assert "Party" in vocab.surfaces["Parties"].collides_with
    assert any(c["surface"] == "Parties" for c in vocab.inflection_collisions)

    node = mk("p/1/1.1", "clause", order=2, label="1.1",
              text="The Parties agree that each Party shall comply.")
    part = mk("p", "part", order=0, title="Part", part_family="core",
              children=[node])
    ms = matching.match_part(part, vocab, sites_list, lambda _n: False,
                             treeio.section_of(part))
    assert {m.ambiguity_kind for m in ms} == {"alias_collision"}
    assert all(m.status == "ambiguous" for m in ms)
