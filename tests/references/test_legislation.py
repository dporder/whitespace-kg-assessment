"""The three legislation shapes SPEC 2.2 says must all parse, and the keys."""
from __future__ import annotations

from pipeline.references import legislation
from pipeline.references.legislation import find_legislation, key_for


def one(text):
    hits = find_legislation(text)
    assert len(hits) == 1, [h.surface for h in hits]
    return hits[0]


def test_title_plus_year():
    hit = one("The Supplier must comply with the Bribery Act 2010 at all times.")
    assert (hit.title, hit.year, hit.instrument_kind) == ("Bribery Act", 2010, "act")
    assert hit.record().key == "legislation/bribery-act-2010"
    assert hit.surface == "Bribery Act 2010"


def test_a_parenthesised_qualifier_belongs_to_the_title():
    """A greedy regex stopping at the first bracket truncates both of these."""
    hit = one("as amended by the European Union (Withdrawal) Act 2018.")
    assert hit.title == "European Union (Withdrawal) Act"
    assert hit.record().key == "legislation/european-union-withdrawal-act-2018"

    hit = one("under the Transfer of Undertakings (Protection of Employment) "
              "Regulations 2006.")
    assert hit.title == "Transfer of Undertakings (Protection of Employment) Regulations"
    assert hit.instrument_kind == "regulations"
    assert hit.record().key == (
        "legislation/transfer-of-undertakings-protection-of-employment-regulations-2006")


def test_the_title_walk_stops_at_the_sentence_not_at_the_bracket():
    hit = one("The Provider must comply with Law. Bribery Act 2010 applies.")
    assert hit.title == "Bribery Act"


def test_a_provision_pointer_becomes_one_ref_per_section():
    hit = one("Sections 55 and 56 of the Patents Act 1977 apply.")
    assert hit.title == "Patents Act"
    assert [p[0] for p in hit.provisions] == ["55", "56"]
    assert hit.provision_unit == "section"
    assert hit.record("section/55").key == "legislation/patents-act-1977/section/55"
    citation = legislation.as_citation(hit)
    assert [m.number for m in citation.members] == ["55", "56"]
    text = "Sections 55 and 56 of the Patents Act 1977 apply."
    assert [text[m.span[0]:m.span[1]] for m in citation.members] == ["55", "56"]


def test_an_eu_instrument_is_its_own_pattern_and_keeps_one_year():
    hit = one("personal data within the meaning of Regulation (EU) 2016/679.")
    assert hit.instrument_kind == "eu_regulation"
    assert hit.title == "Regulation (EU) 2016/679"
    assert hit.record().key == "legislation/regulation-eu-2016-679"
    assert hit.year == 2016


def test_the_eu_year_is_whichever_component_is_a_year():
    assert legislation.eu_year("2016/679") == 2016
    assert legislation.eu_year("1215/2012") == 2012


def test_several_statutes_in_one_sentence_all_parse():
    text = ("the Bribery Act 2010, the Data Protection Act 2018 and Regulation (EU) "
            "2016/679")
    keys = {h.record().key for h in find_legislation(text)}
    assert keys == {"legislation/bribery-act-2010", "legislation/data-protection-act-2018",
                    "legislation/regulation-eu-2016-679"}


def test_normalisation_mints_keys_only():
    """The stored surface is never rewritten by normalisation (CLAUDE.md)."""
    text = "under  the   Bribery   Act 2010"
    hit = one(text)
    assert hit.surface == text[hit.span[0]:hit.span[1]]
    assert "  " in hit.surface, "the source spacing survived"
    assert key_for(hit.title, hit.year) == "legislation/bribery-act-2010"


def test_an_act_word_with_no_year_is_not_a_citation():
    assert find_legislation("The Supplier must act promptly under this Act.") == []
