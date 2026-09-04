"""Typed ambiguity: the four kinds, their precedence, and the heading span."""
from __future__ import annotations

import re

from pipeline.vocabulary import declared, discovery, matching, sites as sites_mod, treeio
from pipeline.vocabulary.text import sentence_initial
from tests.vocabulary.conftest import mk
from tests.vocabulary.test_matching import vocab

BATCHES = {"B2": {"part": "defs-schedule", "pages": (1, 2), "genre": "definitions"}}


def match_one(text: str = None, title: str = None, *, typo_dense: bool = False,
              collisions=None, kind: str = "clause"):
    node = mk("p/1/1.1", kind, order=2, label="1.1", text=text, title=title)
    part = mk("p", "part", order=0, title="Part", part_family="core", children=[node])
    v = vocab(("Widget", "Widget"), ("HB", "Holding Body"), collisions=collisions,
              part="p")
    return matching.match_part(part, v, [], lambda _n: typo_dense,
                               treeio.section_of(part))


def test_sentence_initial_forces_ambiguous():
    m = match_one(text="Widget deliveries must be logged.")[0]
    assert m.status == "ambiguous"
    assert m.ambiguity_kind == "sentence_initial"


def test_mid_sentence_is_confident():
    m = match_one(text="Each Widget must be logged.")[0]
    assert m.status == "confident"
    assert m.ambiguity_kind == "none"


def test_after_a_full_stop_is_sentence_initial():
    ms = match_one(text="Logs are kept. Widget counts are reconciled monthly.")
    assert ms[0].ambiguity_kind == "sentence_initial"


def test_a_heading_match_spans_the_title_not_the_text():
    """SPEC 2.3: char_span offsets into the node's text, or into its title for
    heading matches, which is what the heading ambiguity kind marks."""
    ms = match_one(text="Each Widget must be logged.", title="Widget duties",
                   kind="heading")
    by_field = {m.field_name: m for m in ms}
    assert by_field["title"].ambiguity_kind == "heading"
    assert by_field["title"].span == (0, 6)
    assert by_field["text"].ambiguity_kind == "none"


def test_typo_dense_sections_force_their_own_kind():
    m = match_one(text="Each Widget must be logged.", typo_dense=True)[0]
    assert m.status == "ambiguous"
    assert m.ambiguity_kind == "typo_dense"


def test_alias_collision_when_an_alias_could_bind_to_two_terms():
    m = match_one(text="The Holder shall notify HB of each delivery.",
                  collisions={"HB": ["Handover Body", "Holding Body"]})[0]
    assert m.ambiguity_kind == "alias_collision"
    assert m.collides_with == ["Handover Body", "Holding Body"]


def test_precedence_keeps_the_kind_that_most_changes_the_question():
    """All four can apply at once. The schema field is single-valued, so the
    record keeps the most consequential kind and the routing payload keeps them
    all."""
    ms = match_one(title="HB duties", kind="heading", typo_dense=True,
                   collisions={"HB": ["Handover Body", "Holding Body"]})
    m = ms[0]
    assert set(m.kinds) == {"alias_collision", "typo_dense", "heading",
                            "sentence_initial"}
    assert m.ambiguity_kind == "alias_collision"
    assert matching.AMBIGUITY_PRECEDENCE == (
        "alias_collision", "typo_dense", "heading", "sentence_initial")


def test_a_real_alias_collision_arises_from_two_parts_sharing_an_abbreviation(
        document_definitions_part, clauses_part):
    """`HB` abbreviates Holding Body document-wide and Handover Body inside the
    call-off schedule, so inside that part the abbreviation is genuinely
    ambiguous and the matcher must not pick one."""
    merged = []
    for part, batches in ((document_definitions_part, BATCHES), (clauses_part, {})):
        d = declared.ingest_part(part, batches)
        f = discovery.discover_part(part)
        merged.extend(sites_mod.merge(d, f.sites, f.aliases, {"defs-schedule"})[0])
    v = sites_mod.vocabulary_for("clauses", merged)
    assert v.surfaces["HB"].collides_with == ["Handover Body", "Holding Body"]
    ms = matching.match_part(clauses_part, v, merged, lambda _n: False,
                             treeio.section_of(clauses_part))
    hb = [m for m in ms if m.surface == "HB"]
    assert hb and hb[0].ambiguity_kind == "alias_collision"


# ------------------------------------------------- agreement with stage 8


def test_the_sentence_initial_rule_matches_the_eval_harness():
    """Stage 4 and stage 8 must mean the same thing by "sentence initial", or a
    disagreement between them would look like a pipeline error. Pinned against
    the harness's own implementation rather than a copy of its regex."""
    from pipeline.eval.sections import definitions as eval_definitions
    cases = ["Widget counts.", "Logs are kept. Widget counts.",
             "Each Widget is logged.", "as follows; Widget counts.",
             "the following: Widget counts.", "  Widget counts."]
    for text in cases:
        start = text.index("Widget")
        assert sentence_initial(text, start) == \
            eval_definitions._sentence_initial(text, start), text
