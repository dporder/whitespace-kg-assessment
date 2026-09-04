"""The concept scan: units, prompt contract, and what comes back."""
from __future__ import annotations

import json

import config
from pipeline.concepts import scan as scan_mod
from pipeline.vocabulary import llmio
from tests.concepts.conftest import FakeClaude, concept, install_llm, reply


def runner(tmp_path, enabled=True):
    return llmio.runner("concepts", tmp_path / "run", tmp_path, enabled=enabled)


# ------------------------------------------------------------- the scan unit


def test_the_scan_units_are_parts_and_their_top_level_children(two_part_trees):
    """SPEC 2.4: "a part or top level clause with its full derived subtree
    text"."""
    paths = [u.path for u in scan_mod.units(two_part_trees)]
    assert paths == ["clauses", "clauses/1", "clauses/2", "defs", "defs/1"]


def test_the_units_agree_with_the_ones_stage_8_measures_coverage_over(two_part_trees):
    """Coverage is "sections with zero concepts". If stage 5 and stage 8 counted
    different units, that rate would be measured against a denominator the scan
    never used."""
    from pipeline.eval.sections import concepts as eval_concepts

    class Ctx:
        class inputs:
            trees = two_part_trees.parts
    mine = sorted(u.path for u in scan_mod.units(two_part_trees))
    theirs = sorted(path for _part, path in eval_concepts.scan_units(Ctx))
    assert mine == theirs


def test_the_unit_is_scanned_on_its_full_subtree_text(two_part_trees, tmp_path,
                                                      monkeypatch):
    fake = FakeClaude(reply())
    install_llm(monkeypatch, fake)
    scan_mod.scan(two_part_trees, runner(tmp_path))
    prompt = next(p for _t, p in fake.prompts if "UNIT clauses/1 " in p)
    assert "terminated on notice" in prompt          # 1.1
    assert "hand over the Widget register" in prompt  # 1.2
    assert "Termination" in prompt                    # the heading's own title


def test_the_prompt_lists_the_paths_a_concept_may_claim(two_part_trees, tmp_path,
                                                        monkeypatch):
    fake = FakeClaude(reply())
    install_llm(monkeypatch, fake)
    scan_mod.scan(two_part_trees, runner(tmp_path))
    prompt = next(p for _t, p in fake.prompts if "UNIT clauses/1 " in p)
    assert "clauses/1/1.1 ::" in prompt
    assert "Never invent a path" in prompt


def test_confidence_is_asked_for_before_the_provisions():
    """EVALUATION.md layer 5: scored before the answer is committed."""
    assert scan_mod.PROMPT.index('"confidence"') < scan_mod.PROMPT.index('"provisions"')
    assert "BEFORE listing its provisions" in scan_mod.PROMPT


def test_the_prompt_says_defined_terms_outrank_concepts():
    assert "Defined terms are handled elsewhere and outrank concepts" in scan_mod.PROMPT


def test_the_model_is_the_configured_one():
    assert scan_mod.TASK == "concepts"
    assert config.MODELS[scan_mod.TASK] == "claude-sonnet-5"


# ------------------------------------------------------------- the response


def test_a_proposal_becomes_a_concept_with_its_members(two_part_trees, tmp_path,
                                                       monkeypatch):
    install_llm(monkeypatch, FakeClaude(
        reply(concept("termination triggers", 0.8, ["clauses/1/1.1"]))))
    results = scan_mod.scan(two_part_trees, runner(tmp_path))
    proposed = [c for r in results for c in r.proposed]
    assert proposed
    first = proposed[0]
    assert first.label == "termination triggers"
    assert first.confidence == 0.8
    assert first.member_node_ids == [two_part_trees.by_path()["clauses/1/1.1"].id]


def test_an_invented_path_is_dropped_and_logged(two_part_trees, tmp_path, monkeypatch):
    """Tier 3 never mints a tier 1 node. A path the model made up is not a new
    provision, it is a mistake to record."""
    install_llm(monkeypatch, FakeClaude(
        reply(concept("exit planning", 0.7, ["clauses/1/1.1", "clauses/9/9.9"]))))
    results = scan_mod.scan(two_part_trees, runner(tmp_path))
    dropped = [p for r in results for p in r.dropped_paths]
    assert "clauses/9/9.9" in dropped
    proposed = [c for r in results for c in r.proposed]
    assert all("clauses/9/9.9" not in c.member_paths for c in proposed)


def test_a_concept_with_no_valid_member_is_not_proposed(two_part_trees, tmp_path,
                                                        monkeypatch):
    install_llm(monkeypatch, FakeClaude(
        reply(concept("invented", 0.9, ["nowhere/1"]))))
    results = scan_mod.scan(two_part_trees, runner(tmp_path))
    assert [c for r in results for c in r.proposed] == []


def test_the_relation_field_is_a_verb_and_label_is_accepted_as_a_synonym(
        two_part_trees, tmp_path, monkeypatch):
    """The prompt asks for `relation`; live Sonnet 5 output used `label` and put
    the source concept's own name in it. Parsing accepts both keys, and
    resolve.py is what rejects a verb that is really a concept label."""
    install_llm(monkeypatch, FakeClaude(json.dumps({"concepts": [
        {"label": "a", "confidence": 0.5, "provisions": ["clauses/1/1.1"],
         "relations": [{"relation": "depends_on", "to": "b"},
                       {"label": "constrains", "to": "c"},
                       {"to": "d"}]}]})))
    results = scan_mod.scan(two_part_trees, runner(tmp_path))
    relations = [r for x in results for c in x.proposed for r in c.relations]
    assert {(r["relation"], r["to"]) for r in relations} == \
        {("depends_on", "b"), ("constrains", "c")}


def test_the_prompt_says_a_relation_is_a_verb_not_a_concept_label():
    assert "VERB PHRASE" in scan_mod.PROMPT
    assert "never a concept label" in scan_mod.PROMPT


def test_a_missing_confidence_is_zero_not_assumed_high(two_part_trees, tmp_path,
                                                       monkeypatch):
    install_llm(monkeypatch, FakeClaude(
        json.dumps({"concepts": [{"label": "x", "provisions": ["clauses/1/1.1"]}]})))
    results = scan_mod.scan(two_part_trees, runner(tmp_path))
    assert [c.confidence for r in results for c in r.proposed][0] == 0.0


def test_a_fenced_json_reply_is_still_read(two_part_trees, tmp_path, monkeypatch):
    install_llm(monkeypatch, FakeClaude(
        "```json\n" + reply(concept("termination triggers", 0.8,
                                    ["clauses/1/1.1"])) + "\n```"))
    results = scan_mod.scan(two_part_trees, runner(tmp_path))
    assert [c.label for r in results for c in r.proposed][0] == "termination triggers"


def test_an_unparseable_reply_fails_the_unit_not_the_run(two_part_trees, tmp_path,
                                                         monkeypatch):
    install_llm(monkeypatch, FakeClaude("sorry, I cannot do that"))
    results = scan_mod.scan(two_part_trees, runner(tmp_path))
    assert all(r.parse_error for r in results)
    assert all(r.proposed == [] for r in results)


def test_concept_ids_are_deterministic_in_scope_and_label():
    a = scan_mod.concept_id("core-terms/9", "intellectual property ownership")
    b = scan_mod.concept_id("core-terms/9", "Intellectual Property Ownership ")
    c = scan_mod.concept_id("core-terms/3", "intellectual property ownership")
    assert a == b, "same concept, same id, so a rerun updates rather than twins"
    assert a != c


# ------------------------------------------------------------------ pending


def test_without_llm_py_every_unit_is_queued_with_its_prompt(two_part_trees, tmp_path,
                                                             monkeypatch):
    from tests.vocabulary.llm_seam import without_llm
    without_llm(monkeypatch)
    results = scan_mod.scan(two_part_trees, runner(tmp_path))
    assert all(r.state == llmio.PENDING_MODULE for r in results)
    assert all(r.prompt for r in results)
    assert all(r.proposed == [] for r in results)


def test_a_second_run_delegates_again_and_pipeline_llm_owns_the_cache(
        two_part_trees, tmp_path, monkeypatch):
    """One LLM path: pipeline.llm serves its own replay cache, so this seam
    calls it every time rather than shadowing it with a second cache."""
    fake = FakeClaude(reply(concept("termination triggers", 0.8, ["clauses/1/1.1"])))
    install_llm(monkeypatch, fake)
    scan_mod.scan(two_part_trees, runner(tmp_path))
    calls = len(fake.prompts)
    results = scan_mod.scan(two_part_trees, runner(tmp_path))
    assert len(fake.prompts) == calls * 2
    assert all(r.state == llmio.DELEGATED for r in results)
    assert [c for r in results for c in r.proposed]
