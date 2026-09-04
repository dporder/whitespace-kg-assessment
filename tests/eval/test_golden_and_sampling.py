"""The golden label reader and the stratified sampler.

Both are the parts of the harness that must not assume scale: ten labels and
ten thousand take the same path, and a sample is reproducible without a clock.
"""
from __future__ import annotations

import json

from pipeline.eval import golden as golden_mod
from pipeline.eval.rates import Rate
from pipeline.eval.sampling import (position_bucket, stratified_sample,
                                    word_count_bucket)


# ------------------------------------------------------------------ golden

def test_absent_directory_is_absent_not_empty(tmp_path):
    result = golden_mod.load(tmp_path / "nope")
    assert result.state == "absent"
    assert result.empty


def test_ref_path_carries_its_own_span(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "decisions.jsonl").write_text(json.dumps({
        "kind": "ref", "path": "core-terms/9/9.1/intro/ref@11-23", "verdict": "target",
        "chosen_candidate": "core-terms/3", "reviewer": "dan", "ts": "2026-09-04T00:00:00Z",
    }) + "\n")
    rec = golden_mod.load(tmp_path).records[0]
    assert rec.parent_path == "core-terms/9/9.1/intro"
    assert rec.span == (11, 23)


def test_node_id_plus_char_span_is_an_equally_valid_subject(tmp_path):
    (tmp_path / "decisions.jsonl").write_text(json.dumps({
        "kind": "term", "node_id": "abc123", "char_span": [4, 12], "verdict": "use",
        "chosen_candidate": "Provider", "reviewer": "dan", "ts": "t"}) + "\n")
    rec = golden_mod.load(tmp_path).records[0]
    assert rec.node_id == "abc123" and rec.span == (4, 12)


def test_last_record_for_a_subject_wins(tmp_path):
    subject = {"kind": "ref", "path": "a/ref@0-1", "reviewer": "dan", "ts": "t"}
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps({**subject, "verdict": "target", "chosen_candidate": "x"}) + "\n"
        + json.dumps({**subject, "verdict": "unresolvable"}) + "\n")
    result = golden_mod.load(tmp_path)
    assert len(result.records) == 1
    assert result.records[0].verdict == "unresolvable"
    assert result.superseded == 1


def test_malformed_lines_are_counted_with_their_location_not_dropped(tmp_path):
    (tmp_path / "decisions.jsonl").write_text(
        "{not json}\n"
        + json.dumps({"kind": "ref", "verdict": "target"}) + "\n"
        + json.dumps({"kind": "wat", "path": "a", "verdict": "target"}) + "\n"
        + json.dumps({"kind": "ref", "path": "a/ref@0-1", "verdict": "maybe"}) + "\n")
    result = golden_mod.load(tmp_path)
    assert len(result.malformed) == 3
    assert result.malformed[0]["line"] == 1
    assert "identifies no subject" in result.malformed[1]["error"]
    assert result.unknown_verdicts[0]["verdict"] == "maybe"
    assert result.records == []


def test_unknown_extra_keys_are_ignored(tmp_path):
    (tmp_path / "decisions.jsonl").write_text(json.dumps({
        "kind": "ref", "path": "a/ref@0-1", "verdict": "unresolvable",
        "reviewer": "dan", "ts": "t", "ui_row_id": 17, "note": "hmm"}) + "\n")
    rec = golden_mod.load(tmp_path).records[0]
    assert rec.note == "hmm"


def test_every_documented_verdict_is_accepted(tmp_path):
    lines = []
    for kind, verdicts in golden_mod.VERDICTS.items():
        for i, verdict in enumerate(sorted(verdicts)):
            record = {"kind": kind, "path": f"{kind}/{i}/ref@0-1",
                      "verdict": verdict, "reviewer": "d", "ts": "t"}
            if verdict in golden_mod.REQUIRES_CHOSEN_CANDIDATE.get(kind, ()):
                record["chosen_candidate"] = "somewhere"
            if verdict in golden_mod.REQUIRES_ANOMALY_INDEX.get(kind, ()):
                record["anomaly_index"] = 0
            lines.append(json.dumps(record))
    (tmp_path / "decisions.jsonl").write_text("\n".join(lines) + "\n")
    result = golden_mod.load(tmp_path)
    assert result.unknown_verdicts == []
    assert result.malformed == []
    assert len(result.records) == sum(len(v) for v in golden_mod.VERDICTS.values())


# ------------------------------------- required fields, reviewer blocker 3

def test_a_ref_target_without_chosen_candidate_is_malformed_not_scored(tmp_path):
    """Scoring it would count a label defect as a parser failure."""
    (tmp_path / "decisions.jsonl").write_text(json.dumps({
        "kind": "ref", "path": "core-terms/9/9.1/intro/ref@11-23", "verdict": "target",
        "reviewer": "dan", "ts": "t"}) + "\n")
    result = golden_mod.load(tmp_path)
    assert result.records == []
    assert len(result.malformed) == 1
    assert "requires chosen_candidate" in result.malformed[0]["error"]
    assert result.malformed[0]["line"] == 1


def test_a_term_use_without_chosen_candidate_is_malformed_not_self_graded(tmp_path):
    """Scoring it would adopt the pipeline's own answer as ground truth."""
    (tmp_path / "decisions.jsonl").write_text(json.dumps({
        "kind": "term", "node_id": "abc", "char_span": [0, 3], "verdict": "use",
        "reviewer": "dan", "ts": "t"}) + "\n")
    result = golden_mod.load(tmp_path)
    assert result.records == []
    assert "requires chosen_candidate" in result.malformed[0]["error"]


def test_verdicts_that_assert_no_answer_need_no_chosen_candidate(tmp_path):
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps({"kind": "ref", "path": "a/ref@0-1", "verdict": "unresolvable",
                    "reviewer": "d", "ts": "t"}) + "\n"
        + json.dumps({"kind": "ref", "path": "b/ref@0-1", "verdict": "not_a_reference",
                      "reviewer": "d", "ts": "t"}) + "\n"
        + json.dumps({"kind": "term", "node_id": "n", "char_span": [0, 1],
                      "verdict": "not_a_use", "reviewer": "d", "ts": "t"}) + "\n")
    result = golden_mod.load(tmp_path)
    assert len(result.records) == 3
    assert result.malformed == []


# ------------------------------- anomaly_index, the UI seam's subject key

def test_two_anomalies_on_one_node_hold_two_verdicts(tmp_path):
    """Without the index in the subject, the second record silently replaced
    the first and one reviewer decision was lost."""
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps({"kind": "anomaly", "path": "core-terms/9/9.2", "anomaly_index": 0,
                    "verdict": "confirmed", "reviewer": "dan", "ts": "t"}) + "\n"
        + json.dumps({"kind": "anomaly", "path": "core-terms/9/9.2", "anomaly_index": 1,
                      "verdict": "rejected", "reviewer": "dan", "ts": "t"}) + "\n")
    result = golden_mod.load(tmp_path)
    assert len(result.records) == 2, "both decisions must survive"
    assert result.superseded == 0
    assert {r.anomaly_index for r in result.records} == {0, 1}
    assert {r.verdict for r in result.records} == {"confirmed", "rejected"}
    assert len({r.subject for r in result.records}) == 2


def test_a_second_verdict_on_the_same_anomaly_still_supersedes(tmp_path):
    same = {"kind": "anomaly", "path": "core-terms/9/9.2", "anomaly_index": 0,
            "reviewer": "dan", "ts": "t"}
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps({**same, "verdict": "confirmed"}) + "\n"
        + json.dumps({**same, "verdict": "rejected"}) + "\n")
    result = golden_mod.load(tmp_path)
    assert len(result.records) == 1 and result.records[0].verdict == "rejected"
    assert result.superseded == 1


def test_a_node_anomaly_verdict_without_an_index_is_malformed(tmp_path):
    (tmp_path / "decisions.jsonl").write_text(json.dumps({
        "kind": "anomaly", "path": "core-terms/9/9.2", "verdict": "confirmed",
        "reviewer": "dan", "ts": "t"}) + "\n")
    result = golden_mod.load(tmp_path)
    assert result.records == []
    assert "requires anomaly_index" in result.malformed[0]["error"]


def test_outline_triage_verdicts_need_no_anomaly_index(tmp_path):
    """Their subject is the queue id this harness prints, not a node anomaly."""
    (tmp_path / "decisions.jsonl").write_text(json.dumps({
        "kind": "anomaly", "path": "outline:core-terms#12", "verdict": "outline_wrong",
        "reviewer": "dan", "ts": "t"}) + "\n")
    result = golden_mod.load(tmp_path)
    assert len(result.records) == 1
    assert result.malformed == []


def test_sibling_label_files_are_read_with_decisions_first(tmp_path):
    (tmp_path / "decisions.jsonl").write_text(json.dumps(
        {"kind": "ref", "path": "a/ref@0-1", "verdict": "target",
         "chosen_candidate": "x", "reviewer": "dan", "ts": "t"}) + "\n")
    (tmp_path / "starter.jsonl").write_text(json.dumps(
        {"kind": "term", "node_id": "n", "char_span": [0, 4], "verdict": "use",
         "chosen_candidate": "Provider", "reviewer": "dan", "ts": "t"}) + "\n")
    result = golden_mod.load(tmp_path)
    assert [p.rsplit("/", 1)[-1] for p in result.files] == ["decisions.jsonl",
                                                            "starter.jsonl"]
    assert len(result.records) == 2


# ---------------------------------------------------------------- sampling

def items(n):
    return [{"part": f"p{i % 3}", "size": "1 word" if i % 2 else "2 words", "i": i}
            for i in range(n)]


def key(item):
    return (item["part"], item["size"])


def test_sampling_is_deterministic_without_a_clock():
    a = stratified_sample(items(500), key, 40, ["part", "size"], "seed")
    b = stratified_sample(items(500), key, 40, ["part", "size"], "seed")
    assert a.indices == b.indices
    assert a.seed == b.seed


def test_a_different_population_gives_a_different_but_reproducible_sample():
    a = stratified_sample(items(500), key, 40, ["part", "size"], "seed")
    b = stratified_sample(items(600), key, 40, ["part", "size"], "seed")
    assert a.seed != b.seed
    assert b.indices == stratified_sample(items(600), key, 40,
                                          ["part", "size"], "seed").indices


def test_allocation_is_proportional_to_stratum_population():
    population = ([{"part": "big", "size": "1 word"}] * 90
                  + [{"part": "small", "size": "1 word"}] * 10)
    result = stratified_sample(population, key, 20, ["part", "size"], "s")
    by_key = {c.key[0]: c for c in result.strata}
    assert by_key["big"].allocated == 18
    assert by_key["small"].allocated == 2
    assert result.size == 20


def test_the_same_code_path_serves_ten_and_ten_thousand():
    small = stratified_sample(items(10), key, 40, ["part", "size"], "s")
    large = stratified_sample(items(10_000), key, 40, ["part", "size"], "s")
    assert small.size == 10, "smaller than the request: take everything, say so"
    assert small.requested == 40
    assert large.size == 40
    assert len(set(large.indices)) == 40


def test_an_empty_population_draws_nothing_rather_than_erroring():
    result = stratified_sample([], key, 40, ["part", "size"], "s")
    assert result.size == 0 and result.strata == []


def test_stratum_buckets():
    assert word_count_bucket("Provider") == "1 word"
    assert word_count_bucket("Central Buying Office") == "3+ words"
    assert position_bucket(0, 30) == "early"
    assert position_bucket(29, 30) == "late"
    assert position_bucket(0, 1) == "only"


# -------------------------------------------------------------------- rates

def test_a_rate_is_never_printed_without_its_counts():
    assert str(Rate(9, 10)) == "9/10 (0.900)"
    assert str(Rate(900, 1000)) == "900/1000 (0.900)"


def test_a_rate_over_nothing_is_unknown_not_zero_and_not_one():
    empty = Rate(0, 0)
    assert empty.rate is None
    assert empty.has_data is False
    assert str(empty) == "0/0 (no data)"
    assert empty.as_dict() == {"count": 0, "of": 0, "rate": None}
