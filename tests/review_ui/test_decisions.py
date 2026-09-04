"""golden/decisions.jsonl, checked against the harness's own contract.

The vocabulary is not ours. Where `pipeline/eval/golden.py` is importable these
tests compare against ITS tables rather than restating them, so the two sides
cannot drift again without a failure here. Every test redirects the file into
tmp_path, so no test run ever writes a decision into the repo.
"""
import importlib.util
import json
from pathlib import Path

import pytest

import review_decisions as D

# --------------------------------------------------------------------------
# the harness's reader, when it is on disk
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
_CANDIDATES = [
    ROOT / "pipeline" / "eval" / "golden.py",
    ROOT.parent / "agent-aa2cc72acf0fe96d2" / "pipeline" / "eval" / "golden.py",
]
GOLDEN_PY = next((p for p in _CANDIDATES if p.exists()), None)


def _load_reader():
    import sys

    name = "eval_golden_reader"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, GOLDEN_PY)
    mod = importlib.util.module_from_spec(spec)
    # registered before exec: its dataclasses carry string annotations, which
    # resolve through sys.modules
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


reader = pytest.mark.skipif(GOLDEN_PY is None, reason="eval harness not merged yet")

_ORIGINAL_PATH = D.path        # captured before the autouse fixture redirects it


@pytest.fixture(autouse=True)
def tmp_decisions(tmp_path, monkeypatch):
    target = tmp_path / "golden" / "decisions.jsonl"
    monkeypatch.setattr(D, "path", lambda: target)
    return target


REF = {"kind": "ref", "path": "core-terms/9/9.2/ref@111-121", "verdict": "target",
       "chosen_candidate": "framework-schedule-2", "reviewer": "dan"}
UNRES = {"kind": "ref", "path": "core-terms/9/9.1/intro/ref@28-71",
         "verdict": "unresolvable", "reviewer": "dan"}
NOTREF = {"kind": "ref", "path": "core-terms/9/9.2/ref@130-146",
          "verdict": "not_a_reference", "reviewer": "dan"}
TERM = {"kind": "term", "node_id": "3effa779" * 5, "char_span": [0, 21],
        "verdict": "use", "chosen_candidate": "Good Working Practice", "reviewer": "dan"}
NOTUSE = {"kind": "term", "node_id": "b596b236" * 5, "char_span": [4, 12],
          "verdict": "not_a_use", "reviewer": "dan"}
ANOM = {"kind": "anomaly", "node_id": "12055022" * 5, "verdict": "confirmed",
        "anomaly": "stray_character_in_label: 'rFramework' for 'Framework', recorded verbatim",
        "anomaly_index": 0, "reviewer": "dan"}
ANOM_REJ = {"kind": "anomaly", "node_id": "af9d5756" * 5, "verdict": "rejected",
            "anomaly": "numbering_gap_after_9.2: 9.4 follows in source order",
            "anomaly_index": 0, "reviewer": "dan"}

ALL = [REF, UNRES, NOTREF, TERM, NOTUSE, ANOM, ANOM_REJ]


# --------------------------------------------------------------------------
# the vocabulary is the harness's
# --------------------------------------------------------------------------
@reader
def test_our_verdicts_are_exactly_the_harness_verdicts():
    """The defect that blocked the first review: two vocabularies, zero overlap."""
    theirs = _load_reader().VERDICTS
    ours = {k: set(v) for k, v in D.VERDICTS.items()}
    assert ours == {k: set(v) for k, v in theirs.items()}


@reader
def test_every_verdict_we_can_write_loads_as_recognised(tmp_decisions):
    for d in ALL:
        D.append(dict(d))
    g = _load_reader().load(tmp_decisions.parent)
    assert g.state == "loaded"
    assert g.unknown_verdicts == [], g.unknown_verdicts
    assert g.malformed == [], g.malformed
    assert len(g.records) == len(ALL)


@reader
def test_each_kind_survives_the_readers_subject_resolution(tmp_decisions):
    for d in ALL:
        D.append(dict(d))
    g = _load_reader().load(tmp_decisions.parent)
    assert {r.kind for r in g.records} == {"ref", "term", "anomaly"}
    ref = next(r for r in g.records if r.path == REF["path"])
    assert ref.span == (111, 121), "the reader recovers the span from the ref path"
    assert ref.parent_path == "core-terms/9/9.2"
    assert ref.chosen_candidate == "framework-schedule-2"
    term = next(r for r in g.records if r.kind == "term" and r.verdict == "use")
    assert term.span == (0, 21)
    assert term.chosen_candidate == "Good Working Practice", "governing term, not a stray key"


@reader
def test_our_target_key_agrees_with_the_readers_subject(tmp_decisions):
    """Our queue-row id and the harness's subject must partition alike, or a
    row shows one verdict while the harness scores another."""
    for d in ALL:
        D.append(dict(d))
    g = _load_reader().load(tmp_decisions.parent)
    assert len({r.subject for r in g.records}) == len({D.target_key(d) for d in ALL})


# --------------------------------------------------------------------------
# shape
# --------------------------------------------------------------------------
def test_a_decision_carries_kind_verdict_reviewer_and_ts():
    stored = D.append(dict(REF))
    assert stored["kind"] == "ref" and stored["verdict"] == "target"
    assert stored["reviewer"] == "dan"
    assert stored["ts"].endswith("Z") and len(stored["ts"]) == 20


def test_the_file_is_one_json_object_per_line(tmp_decisions):
    for d in ALL:
        D.append(dict(d))
    lines = tmp_decisions.read_text().splitlines()
    assert len(lines) == len(ALL)
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_appending_never_rewrites_earlier_lines(tmp_decisions):
    D.append(dict(REF))
    first = tmp_decisions.read_text()
    D.append(dict(TERM))
    assert tmp_decisions.read_text().startswith(first)


def test_the_directory_is_created_on_first_write(tmp_decisions):
    assert not tmp_decisions.parent.exists()
    D.append(dict(REF))
    assert tmp_decisions.exists()


def test_the_decisions_path_is_redirectable_for_demos(tmp_path, monkeypatch):
    """SPEC 6: demo and test flows write only to temporary paths, so a
    decisions.jsonl in the repo always holds real verdicts."""
    target = tmp_path / "demo.jsonl"
    monkeypatch.setenv(D.PATH_ENV, str(target))
    assert _ORIGINAL_PATH() == target        # the real resolver, not the fixture's stub
    monkeypatch.delenv(D.PATH_ENV)
    assert _ORIGINAL_PATH().name == "decisions.jsonl"
    assert _ORIGINAL_PATH().parent.name == "golden"


# --------------------------------------------------------------------------
# chosen_candidate rules
# --------------------------------------------------------------------------
def test_ref_target_requires_the_accepted_target_path():
    with pytest.raises(ValueError, match="chosen_candidate"):
        D.append({"kind": "ref", "path": "p", "verdict": "target", "reviewer": "dan"})


def test_term_use_requires_the_governing_term():
    with pytest.raises(ValueError, match="chosen_candidate"):
        D.append({"kind": "term", "node_id": "n", "char_span": [0, 3],
                  "verdict": "use", "reviewer": "dan"})


def test_the_governing_term_may_differ_from_the_matched_one():
    """The alias-collision case the vocabulary exists for."""
    stored = D.append({**TERM, "chosen_candidate": "Central Buying Office"})
    assert stored["chosen_candidate"] == "Central Buying Office"


@pytest.mark.parametrize("d", [UNRES, NOTREF, NOTUSE, ANOM])
def test_chosen_candidate_is_refused_where_it_has_no_meaning(d):
    with pytest.raises(ValueError, match="no meaning"):
        D.append({**d, "chosen_candidate": "something"})


# --------------------------------------------------------------------------
# anomaly_index is part of the subject, not bookkeeping
# --------------------------------------------------------------------------
def test_an_anomaly_decision_requires_anomaly_index():
    bad = {k: v for k, v in ANOM.items() if k != "anomaly_index"}
    with pytest.raises(ValueError, match="anomaly_index"):
        D.append(bad)


def test_anomaly_index_must_be_an_int_not_a_string():
    with pytest.raises(ValueError, match="anomaly_index"):
        D.append({**ANOM, "anomaly_index": "0"})


def test_two_anomalies_on_one_node_are_separate_subjects():
    """Proven regression: without the index they superseded each other."""
    node = "c0ffee00" * 5
    a = D.append({"kind": "anomaly", "node_id": node, "verdict": "confirmed",
                  "anomaly": "first", "anomaly_index": 0, "reviewer": "dan"})
    b = D.append({"kind": "anomaly", "node_id": node, "verdict": "rejected",
                  "anomaly": "second", "anomaly_index": 1, "reviewer": "dan"})
    assert D.target_key(a) != D.target_key(b)
    kept = D.decisions_by_target()
    assert len(kept) == 2, "one verdict was silently lost"


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad, why",
    [
        ({"kind": "clause", "verdict": "target", "path": "p"}, "kind"),
        ({"kind": "ref", "verdict": "approve", "path": "p"}, "verdict"),
        ({"kind": "ref", "verdict": "reject", "path": "p"}, "verdict"),
        ({"kind": "anomaly", "verdict": "reject", "node_id": "n",
          "anomaly": "a", "anomaly_index": 0}, "verdict"),
        ({"kind": "term", "verdict": "confirmed", "node_id": "n", "char_span": [0, 1]}, "verdict"),
        ({"kind": "ref", "verdict": "unresolvable"}, "path"),
        ({"kind": "term", "verdict": "not_a_use", "node_id": "n"}, "char_span"),
        ({"kind": "term", "verdict": "not_a_use", "char_span": [0, 1]}, "node_id"),
        ({"kind": "anomaly", "verdict": "confirmed", "node_id": "n", "anomaly_index": 0}, "anomaly"),
    ],
)
def test_a_malformed_decision_is_refused(bad, why):
    with pytest.raises(ValueError, match=why):
        D.append({**bad, "reviewer": "dan"})


def test_the_old_invented_vocabulary_is_now_refused():
    """approve/reject were ours, not the harness's. They must never load again."""
    for verdict in ("approve", "reject"):
        with pytest.raises(ValueError, match="verdict"):
            D.append({"kind": "ref", "path": "p", "verdict": verdict, "reviewer": "dan"})


def test_a_refused_decision_writes_nothing(tmp_decisions):
    with pytest.raises(ValueError):
        D.append({"kind": "ref", "verdict": "approve", "path": "p", "reviewer": "dan"})
    assert not tmp_decisions.exists()


def test_reviewer_is_required():
    with pytest.raises(ValueError, match="reviewer"):
        D.append({**REF, "reviewer": ""})


# --------------------------------------------------------------------------
# reading back
# --------------------------------------------------------------------------
def test_read_all_round_trips():
    for d in ALL:
        D.append(dict(d))
    assert [r["verdict"] for r in D.read_all()] == [d["verdict"] for d in ALL]


def test_read_all_on_a_missing_file_is_empty():
    assert D.read_all() == []


def test_a_corrupt_line_names_its_line_number(tmp_decisions):
    tmp_decisions.parent.mkdir(parents=True)
    tmp_decisions.write_text('{"kind":"ref"}\nnot json\n')
    with pytest.raises(ValueError, match=":2"):
        D.read_all()


def test_target_key_matches_the_queue_row_id():
    assert D.target_key(D.append(dict(REF))) == REF["path"]
    assert D.target_key(D.append(dict(TERM))) == f"{TERM['node_id']}:0-21"
    assert D.target_key(D.append(dict(ANOM))) == f"{ANOM['node_id']}#0"


def test_the_latest_decision_wins():
    D.append(dict(REF))
    D.append({"kind": "ref", "path": REF["path"], "verdict": "unresolvable", "reviewer": "sam"})
    latest = D.decisions_by_target()[REF["path"]]
    assert latest["verdict"] == "unresolvable" and latest["reviewer"] == "sam"


def test_summary_counts_by_kind_and_verdict():
    for d in ALL:
        D.append(dict(d))
    s = D.summary()
    assert s["count"] == 7
    assert s["by_kind"] == {"ref": 3, "term": 2, "anomaly": 2}
    assert s["by_verdict"]["target"] == 1 and s["by_verdict"]["unresolvable"] == 1
    assert s["vocabulary"]["ref"] == list(D.VERDICTS["ref"])
