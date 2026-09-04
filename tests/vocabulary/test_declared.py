"""Declared ingestion: the definitions the document sets out."""
from __future__ import annotations

import config
from pipeline.vocabulary import declared
from pipeline.vocabulary.text import quote_shape, term_key

BATCHES = {"B2": {"part": "defs-schedule", "pages": (1, 2), "genre": "definitions"}}


def sites_of(part, batches=BATCHES):
    return {s.term: s for s in declared.ingest_part(part, batches)}


# ------------------------------------------------------ the source's defects


def test_closing_quote_without_an_opening_one_is_tolerated(document_definitions_part):
    """206 term cells in the real Joint Schedule 1 print `Term"`. The key must
    come out clean without the parser being told the quote is balanced."""
    sites = sites_of(document_definitions_part)
    assert "Widget" in sites
    assert sites["Widget"].raw_term_text == 'Widget"'
    assert "term_cell_closing_quote_without_opening" in sites["Widget"].anomalies


def test_a_term_missing_its_first_letter_is_never_completed(document_definitions_part):
    """`nsurances` is Insurances with the I genuinely absent from the page.
    Recording it as `Insurances` would be repairing the document."""
    sites = sites_of(document_definitions_part)
    assert "nsurances" in sites
    assert "Insurances" not in sites
    site = sites["nsurances"]
    assert site.raw_term_text == 'nsurances"'
    assert "term_cell_starts_lowercase_first_letter_absent_in_source" in site.anomalies


def test_key_normalisation_never_touches_the_letters():
    assert term_key('  "Widget Register" \n ') == "Widget Register"
    assert term_key('nsurances"') == "nsurances"          # not "Insurances"
    assert term_key('Call-Of Contract"') == "Call-Of Contract"   # not "Call-Off"
    assert quote_shape('Widget"') == "closing_only"
    assert quote_shape('"Widget"') == "both"
    assert quote_shape("Widget") == "none"


# --------------------------------------------------------------- scope


def test_document_scope_comes_from_the_lead_in(document_definitions_part):
    sites = sites_of(document_definitions_part)
    assert sites["Widget"].scope == "document"
    assert sites["Widget"].scope_source == "cue"
    assert "In each Contract" in (sites["Widget"].cue_text or "")


def test_part_local_scope_comes_from_the_lead_in(clauses_part):
    """"In this Schedule" scopes the block to its part, and the part is not the
    definitions schedule, so nothing about the part id is doing the work."""
    sites = sites_of(clauses_part, {})
    assert sites["Widget"].scope == "part:clauses"
    assert sites["Widget"].scope_source == "cue"


def test_part_identity_is_only_the_fallback(document_definitions_part):
    """Strip the lead-in and the config genre decides instead, and says so."""
    head = document_definitions_part.children[0]
    head.children = [c for c in head.children if c.kind != "intro"]
    sites = sites_of(document_definitions_part)
    assert sites["Widget"].scope == "document"
    assert sites["Widget"].scope_source == "part_identity"


def test_the_real_config_names_the_definitions_part():
    """The genre lookup is config-driven, not a hardcoded part id."""
    genres = {b["part"]: b["genre"] for b in config.BATCHES.values()}
    assert genres.get("joint-schedule-1") == "definitions"


# ------------------------------------------------------ aliases and pointers


def test_a_parenthetical_abbreviation_in_a_term_cell_becomes_an_alias(
        document_definitions_part):
    sites = sites_of(document_definitions_part)
    assert "Holding Body" in sites
    assert sites["Holding Body"].aliases == ["HB"]
    assert "HB" not in sites                       # an alias is not its own term


def test_a_delegating_definition_records_its_pointer(document_definitions_part):
    sites = sites_of(document_definitions_part)
    assert sites["Delegated Item"].pointer == "Schedule 6"


def test_pointer_is_none_when_the_definition_states_rather_than_delegates(
        document_definitions_part):
    assert sites_of(document_definitions_part)["Widget"].pointer is None


# ------------------------------------------------------------ table shape


def test_an_ordinary_two_column_table_is_not_a_definitions_table():
    """A milestones table under no definitions lead-in must not mint vocabulary."""
    from tests.vocabulary.conftest import cell, mk
    cells = [cell("p/1/table/0/0", order=2, row=0, col=0, role="label",
                  text="Stage One"),
             cell("p/1/table/0/1", order=3, row=0, col=1, role="value",
                  text="4 weeks"),
             cell("p/1/table/1/0", order=4, row=1, col=0, role="label",
                  text="Stage Two"),
             cell("p/1/table/1/1", order=5, row=1, col=1, role="value",
                  text="6 weeks")]
    table = mk("p/1/table", "table", order=1, n_rows=2, n_cols=2, children=cells)
    head = mk("p/1", "heading", order=1, label="1", title="Timetable",
              children=[table])
    part = mk("p", "part", order=0, title="Framework Schedule 8 (Timetable)",
              part_family="framework-schedule", children=[head])
    assert declared.ingest_part(part, {}) == []


def test_prose_definitions_inside_a_declared_block_are_declared(prose_part):
    sites = {s.term: s for s in declared.ingest_part(prose_part, {})}
    assert sites["Reference Body"].shape == "prose"
    assert sites["Reference Body"].scope == "part:prose"
