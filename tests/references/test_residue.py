"""The residue call: candidates together, NONE accepted, confidence first.

SPEC 2.2 and 2.4. No test here reaches the network; the fake client stands in
for the SDK, including the refusal the live identity-linked key returns today.
Each test sends exactly the one ref it is about, so a call count means what it
says.
"""
from __future__ import annotations

import config
from pipeline import llm
from pipeline.references import residue
from pipeline.references.build import ref_node
from pipeline.references.corpus import Corpus
from pipeline.references.detect import detect_part
from pipeline.references.resolve import resolve_pointer
from pipeline.schemas import Candidate


def build(part_id, tree, corpus, identity):
    refs, contexts = [], {}
    found = detect_part(part_id, tree)
    for order, pointer in enumerate(found.pointers):
        parent = corpus.node(pointer.parent_path)
        resolution = resolve_pointer(corpus, pointer)
        ref = ref_node(pointer, resolution, parent, identity, order=order, batch_id="B1")
        refs.append(ref)
        contexts[ref.path] = {"parent_path": pointer.parent_path, "part": part_id,
                              "unit_label": parent.unit_label, "sentence": pointer.sentence,
                              "notes": resolution.notes}
    return refs, contexts


def corpus_of(*trees):
    return Corpus.from_trees({t.path: t for t in trees})


def one_ref(part_id, tree, corpus, identity, surface):
    """Just the ref under test, so a call count is unambiguous."""
    refs, contexts = build(part_id, tree, corpus, identity)
    ref = next(r for r in refs if r.text == surface)
    return [ref], {ref.path: contexts[ref.path]}, ref


def bare_schedule(core_terms, corpus, identity):
    core_terms.children[3].text = "See Schedule 2 for detail."
    return one_ref("core-terms", core_terms, corpus, identity, "Schedule 2")


def test_a_chosen_candidate_that_exists_becomes_resolved(core_terms, joint_schedule_1,
                                                         identity, fake_llm, answer_json):
    corpus = corpus_of(core_terms, joint_schedule_1)
    core_terms.children[3].text = "See Schedule 1 for detail."
    refs, contexts, ref = one_ref("core-terms", core_terms, corpus, identity, "Schedule 1")
    assert ref.status == "ambiguous"
    fake_llm([answer_json("joint-schedule-1", 0.82)])
    report = residue.run(refs, contexts, corpus)
    assert (ref.status, ref.target_path, ref.resolver) == ("resolved", "joint-schedule-1",
                                                           "llm")
    assert ref.confidence == 0.82
    assert report["resolved"] == 1


def test_none_keeps_it_unresolved_with_the_candidates_attached(core_terms, identity,
                                                               fake_llm, answer_json):
    corpus = corpus_of(core_terms)
    refs, contexts, ref = bare_schedule(core_terms, corpus, identity)
    before = {c.path for c in ref.candidates}
    fake_llm([answer_json("NONE", 0.2)])
    report = residue.run(refs, contexts, corpus)
    assert ref.status == "unresolved"
    assert ref.target_path is None
    assert {c.path for c in ref.candidates} == before, "candidates were dropped"
    assert (ref.resolver, ref.confidence) == ("llm", 0.2)
    assert report["answered_none"] == 1


def test_a_choice_outside_the_corpus_never_mints_a_target(core_terms, identity,
                                                          fake_llm, answer_json):
    corpus = corpus_of(core_terms)
    refs, contexts, ref = bare_schedule(core_terms, corpus, identity)
    fake_llm([answer_json("call-off-schedule-2", 0.9)])
    residue.run(refs, contexts, corpus)
    assert ref.status == "unresolved"
    assert ref.target_path is None
    assert any("llm_chose_uningested_target" in a for a in ref.anomalies)
    top = max(ref.candidates, key=lambda c: c.score)
    assert top.path == "call-off-schedule-2", "the model's choice was not promoted"


def test_an_off_list_answer_escalates_to_the_harder_model(core_terms, identity,
                                                          fake_llm, answer_json):
    corpus = corpus_of(core_terms)
    refs, contexts, ref = bare_schedule(core_terms, corpus, identity)
    client = fake_llm([answer_json("core-terms/99", 0.9), answer_json("NONE", 0.3)])
    report = residue.run(refs, contexts, corpus)
    assert report["escalated"] == 1
    models = [c["model"] for c in client.messages.calls]
    assert models == [config.MODELS["reference_residue"], config.MODELS["reference_hard"]]
    assert ref.status == "unresolved" and ref.target_path is None


def test_the_named_hard_case_goes_straight_to_the_harder_model(joint_schedule_1,
                                                               core_terms, identity,
                                                               fake_llm, answer_json):
    """The mislabelled "Clause 1.x inside a Schedule" is the hard case DESIGN 10
    says to escalate."""
    corpus = corpus_of(core_terms, joint_schedule_1)
    refs, contexts, ref = one_ref("joint-schedule-1", joint_schedule_1, corpus, identity,
                                  "Clause 1.2")
    assert any(a.startswith("mislabelled_cross_reference") for a in ref.anomalies)
    client = fake_llm([answer_json("joint-schedule-1/1/1.2", 0.7)])
    residue.run(refs, contexts, corpus)
    assert [c["model"] for c in client.messages.calls] == [config.MODELS["reference_hard"]]
    assert (ref.status, ref.target_path) == ("resolved", "joint-schedule-1/1/1.2")


def test_the_prompt_shows_every_candidate_and_asks_for_confidence_first(core_terms,
                                                                       identity):
    corpus = corpus_of(core_terms)
    _refs, contexts, ref = bare_schedule(core_terms, corpus, identity)
    prompt = residue.build_prompt(ref, contexts[ref.path], ref.candidates)
    for candidate in ref.candidates:
        assert candidate.path in prompt
    assert "NONE" in prompt
    assert prompt.index('"confidence"') < prompt.index('"answer"')
    assert "Never invent a path" in prompt


def test_at_most_five_candidates_are_presented(core_terms, identity, fake_llm,
                                               answer_json):
    corpus = corpus_of(core_terms)
    refs, contexts, ref = bare_schedule(core_terms, corpus, identity)
    ref.candidates = [Candidate(path=f"call-off-schedule-{i}", score=0.5 - i / 100,
                                reason="synthetic") for i in range(9)]
    client = fake_llm([answer_json("NONE", 0.1)])
    residue.run(refs, contexts, corpus)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    listed = [line for line in prompt.splitlines()
              if line.strip()[:2] in {f"{i}." for i in range(1, 10)}]
    assert len(listed) == residue.TOP_N


def test_a_refused_key_leaves_the_deterministic_outcome_and_queues_the_ref(
        core_terms, identity, fake_llm, refused):
    """The live key is identity-linked and refused until the workspace id is
    supplied. Nothing may be guessed, nothing may be lost."""
    corpus = corpus_of(core_terms)
    refs, contexts, ref = bare_schedule(core_terms, corpus, identity)
    fake_llm(error=refused())
    report = residue.run(refs, contexts, corpus)
    assert (ref.status, ref.resolver, ref.target_path) == ("ambiguous", "scope", None)
    assert ref.candidates, "the deterministic candidates survived"
    assert any(a.startswith(residue.QUEUE_MARKER) for a in ref.anomalies)
    assert report["queued"] == 1
    assert "anthropic-workspace-id" in report["reason"]
    queue = residue.queue_file(report)
    assert queue["count"] == report["queued"]
    assert queue["items"][0]["ref_path"] == ref.path


def test_no_llm_queues_without_calling(core_terms, identity, fake_llm, answer_json):
    corpus = corpus_of(core_terms)
    refs, contexts, ref = bare_schedule(core_terms, corpus, identity)
    client = fake_llm([answer_json("NONE")])
    report = residue.run(refs, contexts, corpus, no_llm=True)
    assert client.messages.calls == []
    assert report["queued"] == 1 and report["called"] == 0
    assert ref.resolver == "scope"


def test_a_ref_with_no_candidates_goes_to_review_not_to_a_prompt(core_terms, identity,
                                                                 fake_llm, answer_json):
    """A model shown an empty candidate list is being invited to invent one."""
    corpus = corpus_of(core_terms)
    core_terms.children[3].text = "See Section 4 for detail."
    refs, contexts, ref = one_ref("core-terms", core_terms, corpus, identity, "Section 4")
    assert ref.candidates == []
    client = fake_llm([answer_json("NONE")])
    report = residue.run(refs, contexts, corpus)
    assert report["skipped_no_candidates"] == 1
    assert report["considered"] == 0
    assert client.messages.calls == []


def test_the_second_run_replays_from_the_cache(core_terms, identity, fake_llm,
                                               answer_json):
    corpus = corpus_of(core_terms)
    refs, contexts, ref = bare_schedule(core_terms, corpus, identity)
    client = fake_llm([answer_json("NONE", 0.3)])
    residue.run(refs, contexts, corpus)
    refs2, contexts2, ref2 = bare_schedule(core_terms, corpus, identity)
    residue.run(refs2, contexts2, corpus)
    assert len(client.messages.calls) == 1, "an unchanged input hit the API again"
    assert (ref2.status, ref2.confidence, ref2.resolver) == (ref.status, ref.confidence,
                                                             ref.resolver)


def test_every_residue_call_is_logged(core_terms, identity, fake_llm, answer_json):
    corpus = corpus_of(core_terms)
    refs, contexts, _ref = bare_schedule(core_terms, corpus, identity)
    fake_llm([answer_json("NONE", 0.3)])
    residue.run(refs, contexts, corpus)
    logged = list((llm.log_dir() / residue.TASK).glob("*.json"))
    assert len(logged) == 1


# --------------------------------------------------------------------------
# rung three of the ladder: LLM span extraction, orphan sentences only
# --------------------------------------------------------------------------
def orphan_sentences(core_terms):
    found = detect_part("core-terms", core_terms)
    node_text = {}
    stack = [core_terms]
    while stack:
        node = stack.pop()
        node_text[node.path] = node.text or ""
        stack.extend(node.children)
    return found.llm_sentences, node_text


def span_reply(text, kind="unknown", confidence=0.6):
    import json
    return json.dumps({"considered": "weighed it", "confidence": confidence,
                       "answer": [{"text": text, "kind": kind}]})


def test_only_orphan_sentences_are_sent(core_terms, fake_llm):
    """The grammar's hits are never re-asked: rung three sees the residue only."""
    sentences, node_text = orphan_sentences(core_terms)
    assert sentences, "the fixture tree should leave at least one orphan keyword"
    client = fake_llm([span_reply("Table 2", "unknown")] * len(sentences))
    found, report = residue.extract_spans(sentences, node_text)
    assert report["called"] == len(sentences)
    assert len(client.messages.calls) == len(sentences)
    for call in client.messages.calls:
        prompt = call["messages"][0]["content"]
        assert "found nothing" in prompt


def test_an_extracted_span_must_reproduce_its_own_characters(core_terms, fake_llm):
    sentences, node_text = orphan_sentences(core_terms)
    fake_llm([span_reply("Table 2", "unknown")] * len(sentences))
    found, report = residue.extract_spans(sentences, node_text)
    assert report["spans_accepted"] >= 1
    item = found[0]
    start, end = item["span"]
    assert node_text[item["node_path"]][start:end] == item["text"] == "Table 2"


def test_a_span_the_model_invented_is_rejected_with_a_reason(core_terms, fake_llm):
    sentences, node_text = orphan_sentences(core_terms)
    fake_llm([span_reply("Clause 42 of the Nonexistent Act")] * len(sentences))
    found, report = residue.extract_spans(sentences, node_text)
    assert found == []
    assert report["spans_rejected"] >= 1
    assert "not in the sentence" in report["rejections"][0]["reason"]


def test_no_llm_queues_the_orphan_sentences(core_terms, fake_llm):
    sentences, node_text = orphan_sentences(core_terms)
    client = fake_llm([span_reply("Table 2")])
    found, report = residue.extract_spans(sentences, node_text, no_llm=True)
    assert found == [] and report["queued"] == len(sentences)
    assert client.messages.calls == []


def test_the_span_prompt_asks_for_confidence_first_and_allows_nothing_found():
    prompt = residue.build_span_prompt("The Supplier shall play its part.", ["part"])
    assert prompt.index('"confidence"') < prompt.index('"answer"')
    assert "empty list" in prompt
    assert "character for character" in prompt
