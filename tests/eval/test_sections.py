"""Section behaviour on the fixtures: term scoring, cross checks, transitions."""
from __future__ import annotations

import json

import pytest

import config

INTRO_311 = "core-terms/3/3.1/3.1.1/intro"          # "The Provider must supply Outputs:"
ITEM_311B = "core-terms/3/3.1/3.1.1/b"              # "Good Working Practice applies ..."
CLAUSE_92 = "core-terms/9/9.2"                      # "Any New IPR created under ..."
ITEM_91B = "core-terms/9/9.1/b"                     # "... supplied to CBO by a ..."


def node_id(workspace, path, part="core-terms"):
    return workspace.node(part, path)["id"]


# ---------------------------------------------------------------- golden_terms

def test_term_false_positives_and_negatives_are_split_by_ambiguity_kind(workspace):
    workspace.label(kind="term", node_id=node_id(workspace, INTRO_311),
                    char_span=[4, 12], verdict="not_a_use",
                    note="seeded false positive, pipeline kind is none")
    workspace.label(kind="term", node_id=node_id(workspace, ITEM_311B),
                    char_span=[0, 21], verdict="use", chosen_candidate="Good Working Practice")
    workspace.label(kind="term", node_id=node_id(workspace, CLAUSE_92),
                    char_span=[0, 3], verdict="use", chosen_candidate="Any",
                    note="seeded miss, sentence-initial, no pipeline record to read a kind off")
    run = workspace.run()

    section = run.section("golden_terms")
    assert section["status"] == "measured"
    assert section["confusion"] == {"true_positives": 1, "false_positives": 1,
                                    "false_negatives": 1, "wrong_term_counted_as_both": 0}
    assert section["detection_recall"] == {"count": 1, "of": 2, "rate": 0.5}
    assert section["precision_over_labelled_spans"] == {"count": 1, "of": 2, "rate": 0.5}

    per_kind = section["per_ambiguity_kind"]
    assert per_kind["none"]["false_positives"] == 1
    assert per_kind["sentence_initial"]["true_positives"] == 1
    assert per_kind["sentence_initial"]["false_negatives"] == 1, \
        "the missed use's kind is derived by the harness, there is no pipeline record"
    assert per_kind["sentence_initial"]["false_negative_kind_source"] == "eval_derived"


def test_cost_weighted_summary_uses_the_config_placeholders(workspace):
    workspace.label(kind="term", node_id=node_id(workspace, INTRO_311),
                    char_span=[4, 12], verdict="not_a_use")
    workspace.label(kind="term", node_id=node_id(workspace, CLAUSE_92),
                    char_span=[0, 3], verdict="use", chosen_candidate="Any")
    weighted = workspace.run().section("golden_terms")["cost_weighted_summary"]

    fp, fn = config.ERROR_COSTS["term_false_positive"], config.ERROR_COSTS["term_false_negative"]
    assert weighted["false_positives"] == 1 and weighted["false_negatives"] == 1
    assert weighted["weighted_cost"] == pytest.approx(fp + fn)
    assert weighted["false_negative_weight"] > weighted["false_positive_weight"], \
        "EVALUATION.md s2: a hidden obligation costs more than a polluted graph"


def test_the_wrong_term_on_a_real_use_counts_as_both_errors(workspace):
    """The alias use: the pipeline says Central Buying Office, the label says otherwise."""
    workspace.label(kind="term", node_id=node_id(workspace, ITEM_91B),
                    char_span=[32, 35], verdict="use",
                    chosen_candidate="Replacement Provider")
    confusion = workspace.run().section("golden_terms")["confusion"]
    assert confusion["wrong_term_counted_as_both"] == 1
    assert confusion["false_positives"] == 1 and confusion["false_negatives"] == 1
    assert confusion["true_positives"] == 0


def test_no_term_labels_reports_the_population_without_scoring(workspace):
    section = workspace.run().section("golden_terms")
    assert section["status"] == "no_data"
    assert "no golden term labels yet" in section["reason"]
    assert section["pipeline_population"]["term_uses_total"] == 8
    assert section["pipeline_population"]["by_ambiguity_kind"]["sentence_initial"] == 1
    assert "detection_recall" not in section, "nothing is scored without labels"


# ------------------------------------------------------- definitions_vs_provided

def test_discovery_precision_and_recall_against_the_declared_schedule(workspace):
    section = workspace.run().section("definitions_vs_provided")
    assert section["declared_terms"] == 4
    assert section["discovered_terms"] == 1
    assert section["discovery_precision_against_declared"] == {"count": 1, "of": 1,
                                                               "rate": 1.0}
    assert section["discovery_recall_against_declared"] == {"count": 1, "of": 4,
                                                            "rate": 0.25}
    assert section["discovered_outside_the_declared_schedule"]["count"] == 0


def test_a_term_used_with_no_definition_site_is_surfaced(workspace):
    """The fixtures use Outputs twice and define it nowhere: a broken edge."""
    section = workspace.run().section("definitions_vs_provided")
    assert {"term": "Outputs", "uses": 2} in section["term_uses_with_no_definition_site"]


def test_defined_using_graph_is_derived_from_uses_inside_definition_texts(workspace):
    graph = workspace.run().section("definitions_vs_provided")["defined_using_graph"]
    assert graph["status"] == "measured"
    assert graph["cycles"] == []
    assert ["Materials", "Provider"] in [list(e) for e in graph["edge_list"]]
    assert graph["max_chain_depth"] == 2


def test_capitalised_but_never_defined_separates_weak_evidence(workspace):
    found = workspace.run().section("definitions_vs_provided")["capitalised_but_never_defined"]
    phrases = {p["phrase"] for p in found["multi_word_phrases"]["top"]}
    assert "Existing IPRs" in phrases
    assert found["single_word_sentence_initial"]["distinct"] >= 1
    assert "weak evidence" in found["single_word_sentence_initial"]["note"]


# ------------------------------------------------------------ page_map, outline

def test_the_part_count_question_is_answered_with_all_three_witnesses(workspace):
    counts = workspace.run(use_pdf=True).section("page_map_vs_provided")["part_counts"]
    if counts["notes_stated_part_count"] is None:
        pytest.skip("assignment notes not present")
    assert counts["notes_stated_part_count"] == 46
    assert counts["notes_table_rows"] == 48
    assert counts["embedded_outline_top_level_entries"] == 48
    assert counts["derived_count_comparable"] is False, \
        "three fixture parts cannot be compared to a whole-document count"
    assert "the provided counts describe all 475 pages" in counts["not_comparable_reason"]


def test_outline_disagreements_are_unreviewed_until_a_human_rules(workspace):
    first = workspace.run(use_pdf=True)
    section = first.section("outline_vs_provided")
    if section["status"] != "measured":
        pytest.skip("assignment document not present")
    assert section["totals"]["triage"]["parser_wrong"] == 0
    assert section["totals"]["triage"]["unreviewed"] == section["totals"]["disagreements"]
    queue_id = section["triage_queue"][0]["queue_id"]

    workspace.label(kind="anomaly", path=queue_id, verdict="outline_wrong",
                    note="the outline entry points at a heading that is not there")
    second = workspace.run(use_pdf=True).section("outline_vs_provided")
    assert second["totals"]["triage"]["outline_wrong"] == 1
    assert second["totals"]["triage"]["unreviewed"] == \
        section["totals"]["triage"]["unreviewed"] - 1


def test_the_outline_window_comes_from_the_provided_map_not_the_derived_pages(workspace):
    """Fixture pages are fixture-local; using them to pick the comparison set
    would let a page error hide a structure error."""
    section = workspace.run(use_pdf=True).section("outline_vs_provided")
    if section["status"] != "measured":
        pytest.skip("assignment document not present")
    core = next(p for p in section["per_part"] if p["part"] == "core-terms")
    assert core["window"] == [1, 22]
    assert core["window_source"].startswith("provided page map")


# ------------------------------------------------------ resolution_transitions

def test_an_unresolved_reference_flipping_to_resolved_is_counted(workspace):
    """The second-document story: a ref out to a part that had not arrived."""
    first = workspace.run()
    assert first.section("resolution_transitions")["status"] == "no_data", \
        "the first run has no history and must say so, not report zero"

    refs = workspace.refs("core-terms")
    target = next(r for r in refs["refs"] if r["status"] == "unresolved")
    target["status"] = "resolved"
    target["target_path"] = "framework-schedule-4"
    target["resolver"] = "scope"
    target.pop("candidates", None)
    workspace.write_refs("core-terms", refs)

    section = workspace.run().section("resolution_transitions")
    assert section["status"] == "measured"
    assert section["unresolved_to_resolved"] == {"count": 1, "of": 2, "rate": 0.5}
    assert section["per_batch"]["B1"]["unresolved_to_resolved"] == 1
    assert section["per_batch"]["B2"]["unresolved_to_resolved"] == 0
    assert section["flips"][0]["target_path"] == "framework-schedule-4"


def test_the_snapshot_is_written_for_the_next_run(workspace):
    run = workspace.run()
    snapshot = json.loads((run.eval_dir / "ref_status_snapshot.json").read_text())
    assert len(snapshot["refs"]) == 7
    assert snapshot["refs"]["core-terms/9/9.2/ref@111-121"]["status"] == "ambiguous"


# ------------------------------------------------------------------- concepts

def test_concept_coverage_and_duplicates(workspace):
    section = workspace.run().section("concepts")
    assert section["concepts_total"] == 2
    assert section["duplicate_rate_after_resolution"] == {"count": 0, "of": 2, "rate": 0.0}
    assert "lexical proxy" in section["duplicate_method"]
    assert str(config.CONCEPT_MERGE_COSINE) in section["duplicate_method"]
    assert section["coverage"]["of"] == 8
    uncovered = {u["path"] for u in section["scan_units_with_no_concept"]}
    assert "award-form" in uncovered and "joint-schedule-1/2" in uncovered
    assert section["spot_check"]["items"], "a spot check sample is for human eyes"


def test_a_concept_label_colliding_with_a_declared_term_is_reported(workspace):
    concepts = json.loads((workspace.fixtures / "concepts.json").read_text())
    concepts[0]["label"] = "Good Working Practice"
    (workspace.fixtures / "concepts.json").write_text(json.dumps(concepts, indent=2))
    collisions = workspace.run().section("concepts")[
        "concept_label_collides_with_a_declared_term"]
    assert collisions and collisions[0]["term"] == "Good Working Practice"


# -------------------------------------------------------- confidence_calibration

def test_deterministic_resolvers_carry_no_raw_score_and_get_a_class_precision(workspace):
    section = workspace.run().section("confidence_calibration")
    assert section["refs_with_a_raw_score"] == 1
    assert section["refs_without_a_raw_score"] == 6
    for resolver in ("grammar", "scope"):
        entry = section["measured_precision_by_resolver"][resolver]
        assert entry["attachable_at_load"] is False
        assert "vibe, not a number" in entry["note"]


def test_a_golden_label_fills_in_the_class_precision(workspace):
    workspace.label(kind="ref", path="core-terms/9/9.1/intro/ref@11-23",
                    verdict="target", chosen_candidate="core-terms/3/3.1/3.1.2")
    section = workspace.run().section("confidence_calibration")
    scope = section["measured_precision_by_resolver"]["scope"]
    assert scope["measured_precision"] == {"count": 1, "of": 1, "rate": 1.0}
    assert scope["attachable_at_load"] is True


# ------------------------------------------------------------ stratified_audit

def test_the_audit_reports_pending_llm_rather_than_an_agreement_it_did_not_measure():
    """pipeline/llm.py belongs to another worker and does not exist yet."""
    import importlib.util

    from pipeline.eval.sections.stratified_audit import _run_checker

    if importlib.util.find_spec("pipeline.llm") is not None:
        pytest.skip("pipeline/llm.py now exists; this path no longer applies")
    verdicts, note = _run_checker([{"kind": "term_use", "term": "Provider"}])
    assert verdicts is None
    assert note.startswith("audit runner pending llm.py")


def test_the_audit_draws_its_sample_even_though_the_checker_is_missing(workspace):
    section = workspace.run().section("stratified_audit")
    assert section["status"] == "no_data"
    assert section["reason"], "an unmeasured agreement rate must say why"
    assert "agreement" not in section, "nothing is reported that was not measured"
    assert section["config"]["strata"] == config.AUDIT["strata"]
    assert section["config"]["confident_term_sample_size"] == \
        config.AUDIT["confident_term_sample_size"]
    terms = section["samples"]["confident_term_uses"]
    assert terms["population_size"] == 7, "only confident matches are audited"
    assert terms["drawn_sample_size"] == 7, "population below the requested size"
    assert terms["strata"] == ["term_word_count", "part", "position"]
    assert section["sample_items"], "the sample is stored even when unchecked"
