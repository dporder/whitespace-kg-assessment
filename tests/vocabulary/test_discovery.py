"""The discovery rule, and the diff it exists to make possible."""
from __future__ import annotations

from pipeline.vocabulary import declared, discovery, sites as sites_mod
from pipeline.vocabulary.text import initials, is_initialism_of

BATCHES = {"B2": {"part": "defs-schedule", "pages": (1, 2), "genre": "definitions"}}


def test_prose_form_is_discovered(prose_part):
    found = discovery.discover_part(prose_part)
    assert "Reference Body" in {s.term for s in found.sites}


def test_a_definitions_row_is_discovered_only_when_the_verb_is_printed(
        document_definitions_part):
    """The convention is a quoted term followed by a definitional verb. Rows
    whose value cell states the definition without the verb are declared but
    not discovered, which measures the drafters' consistency rather than a
    blind spot in the scanner."""
    terms = {s.term for s in discovery.discover_part(document_definitions_part).sites}
    assert "Widget" in terms                    # "means an item supplied ..."
    assert "Delegated Item" in terms            # "has the meaning given in ..."
    assert "Widget Register" not in terms       # "the list of Widgets kept ..."
    assert "nsurances" not in terms


def test_a_quoted_parenthetical_that_is_not_an_initialism_mints_a_term(prose_part):
    terms = {s.term for s in discovery.discover_part(prose_part).sites}
    assert "Named Papers" in terms


def test_a_parenthetical_initialism_is_an_alias_not_a_term(prose_part):
    found = discovery.discover_part(prose_part)
    assert "CWO" not in {s.term for s in found.sites}
    assert ("CWO", "Central Widget Office") in {(a.alias, a.phrase) for a in found.aliases}


def test_the_initialism_test_itself():
    assert initials("Information and Communication Technology") == "ICT"
    assert is_initialism_of("CBO", "Central Buying Office")
    assert is_initialism_of("ICT", "Information and Communication Technology")
    assert not is_initialism_of("EU References", "the EEA agreement")


# ------------------------------------------------------------------ the diff


def test_declared_and_discovered_stay_separate_and_meet_as_both(
        document_definitions_part):
    declared_sites = declared.ingest_part(document_definitions_part, BATCHES)
    found = discovery.discover_part(document_definitions_part)
    merged, _unattached = sites_mod.merge(declared_sites, found.sites, found.aliases,
                                          {"defs-schedule"})
    by_term = {m.term: m for m in merged}
    assert by_term["Widget"].source == "both"            # both passes named it
    assert by_term["Widget Register"].source == "declared"
    assert by_term["nsurances"].source == "declared"


def test_a_term_only_the_rule_finds_is_discovered(prose_part):
    """`Named Papers` is introduced parenthetically in a clause with no
    definitions lead-in, so no declared pass sees it. That is the case a fixed
    list would miss and the rule exists for."""
    declared_sites = declared.ingest_part(prose_part, {})
    found = discovery.discover_part(prose_part)
    merged, _u = sites_mod.merge(declared_sites, found.sites, found.aliases, set())
    by_term = {m.term: m for m in merged}
    assert by_term["Named Papers"].source == "discovered"
    assert by_term["Named Papers"].scope == "part:prose"


def test_an_alias_attaches_when_the_SHORT_form_is_the_declared_term():
    """`the Crown Commercial Service (CCS)` introduces a pair, and Joint
    Schedule 1 declares the short form. Handling only the long-form direction
    left every real abbreviation in this pack unattached and dropped the long
    forms from the matcher: CCS, ICT, ISMS, NCSC, EIR and CEDR."""
    from tests.vocabulary.conftest import definitions_table, mk
    intro, table, _values = definitions_table(
        "p", "In each Contract, the following words shall have the following "
             "meanings:",
        [('"CCS"', "the authority named in the Order Form;")])
    body = mk("p/2/2.1", "clause", order=20, label="2.1",
              text="The Crown Commercial Service (CCS) shall publish the notice.")
    head = mk("p/1", "heading", order=1, label="1", title="Definitions",
              children=[intro, table])
    head2 = mk("p/2", "heading", order=19, label="2", title="Notices",
               children=[body])
    part = mk("p", "part", order=0, title="Joint Schedule 1 (Definitions)",
              part_family="joint-schedule", children=[head, head2])

    declared_sites = declared.ingest_part(part, {})
    found = discovery.discover_part(part)
    merged, unattached = sites_mod.merge(declared_sites, found.sites, found.aliases,
                                         {"p"})
    site = next(m for m in merged if m.term == "CCS")
    assert "Crown Commercial Service" in site.raw.aliases
    assert unattached == []
    vocab = sites_mod.vocabulary_for("p", merged)
    assert vocab.surfaces["Crown Commercial Service"].term == "CCS"


def test_an_alias_found_in_running_text_attaches_to_its_term():
    """The Crown-Commercial-Service-(CCS) convention: the abbreviation becomes a
    matchable surface on the term it abbreviates, not a term of its own."""
    from tests.vocabulary.conftest import mk
    body = mk("p/1/1.1", "clause", order=2, label="1.1",
              text='In this Schedule, "Central Widget Office" means the office '
                   "named in the Order Form. The Central Widget Office (CWO) "
                   "shall keep the register.")
    head = mk("p/1", "heading", order=1, label="1", title="Interpretation",
              children=[body])
    part = mk("p", "part", order=0, title="Joint Schedule 4 (Widgets)",
              part_family="joint-schedule", children=[head])
    declared_sites = declared.ingest_part(part, {})
    found = discovery.discover_part(part)
    merged, unattached = sites_mod.merge(declared_sites, found.sites, found.aliases, set())
    site = next(m for m in merged if m.term == "Central Widget Office")
    assert "CWO" in site.raw.aliases
    assert unattached == []
