"""golden/decisions.jsonl, the shape stage 8 consumes.

Every test redirects the file into tmp_path, so the real golden set is never
touched by a test run.
"""
import json

import pytest

import review_decisions as D


@pytest.fixture(autouse=True)
def tmp_decisions(tmp_path, monkeypatch):
    target = tmp_path / "golden" / "decisions.jsonl"
    monkeypatch.setattr(D, "path", lambda: target)
    return target


REF = {"kind": "ref", "path": "core-terms/9/9.2/ref@111-121", "verdict": "approve",
       "chosen_candidate": "framework-schedule-2"}
TERM = {"kind": "term", "node_id": "3effa779" * 5, "char_span": [0, 21], "verdict": "reject"}
ANOM = {"kind": "anomaly", "node_id": "12055022" * 5, "verdict": "approve",
        "anomaly": "stray_character_in_label: 'rFramework' for 'Framework', recorded verbatim"}


# ----------------------------------------------------------------- the shape
def test_a_decision_carries_kind_verdict_reviewer_and_ts(tmp_decisions):
    stored = D.append(dict(REF), reviewer="dan")
    assert stored["kind"] == "ref"
    assert stored["verdict"] == "approve"
    assert stored["reviewer"] == "dan"
    assert stored["ts"].endswith("Z") and len(stored["ts"]) == 20


def test_each_kind_carries_its_identity():
    ref = D.append(dict(REF), reviewer="dan")
    assert ref["path"] and "node_id" not in ref

    term = D.append(dict(TERM), reviewer="dan")
    assert term["node_id"] and term["char_span"] == [0, 21]

    anom = D.append(dict(ANOM), reviewer="dan")
    assert anom["node_id"] and anom["anomaly"]


def test_a_ref_approval_may_name_the_chosen_candidate():
    assert D.append(dict(REF), reviewer="dan")["chosen_candidate"] == "framework-schedule-2"


def test_the_file_is_one_json_object_per_line(tmp_decisions):
    for d in (REF, TERM, ANOM):
        D.append(dict(d), reviewer="dan")
    lines = tmp_decisions.read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        obj = json.loads(line)
        assert isinstance(obj, dict)
        assert "\n" not in line


def test_appending_never_rewrites_earlier_lines(tmp_decisions):
    D.append(dict(REF), reviewer="dan")
    first = tmp_decisions.read_text()
    D.append(dict(TERM), reviewer="sam")
    assert tmp_decisions.read_text().startswith(first)


def test_the_directory_is_created_on_first_write(tmp_decisions):
    assert not tmp_decisions.parent.exists()
    D.append(dict(REF), reviewer="dan")
    assert tmp_decisions.exists()


# ------------------------------------------------------------- validation
@pytest.mark.parametrize(
    "bad, why",
    [
        ({"kind": "clause", "verdict": "approve", "path": "p"}, "kind"),
        ({"kind": "ref", "verdict": "maybe", "path": "p"}, "verdict"),
        ({"kind": "ref", "verdict": "approve"}, "path"),
        ({"kind": "term", "verdict": "approve", "node_id": "n"}, "char_span"),
        ({"kind": "term", "verdict": "approve", "node_id": "n", "char_span": [1]}, "char_span"),
        ({"kind": "term", "verdict": "approve", "char_span": [0, 1]}, "node_id"),
        ({"kind": "anomaly", "verdict": "approve", "node_id": "n"}, "anomaly"),
        ({"kind": "ref", "verdict": "reject", "path": "p", "chosen_candidate": "c"}, "chosen_candidate"),
    ],
)
def test_a_malformed_decision_is_refused(bad, why):
    with pytest.raises(ValueError, match=why):
        D.append(dict(bad), reviewer="dan")


def test_a_refused_decision_writes_nothing(tmp_decisions):
    with pytest.raises(ValueError):
        D.append({"kind": "ref", "verdict": "maybe", "path": "p"}, reviewer="dan")
    assert not tmp_decisions.exists()


def test_reviewer_is_required():
    with pytest.raises(ValueError, match="reviewer"):
        D.append(dict(REF), reviewer="")


# ------------------------------------------------------------- reading back
def test_read_all_round_trips():
    for d in (REF, TERM, ANOM):
        D.append(dict(d), reviewer="dan")
    rows = D.read_all()
    assert [r["kind"] for r in rows] == ["ref", "term", "anomaly"]


def test_read_all_on_a_missing_file_is_empty():
    assert D.read_all() == []


def test_a_corrupt_line_names_its_line_number(tmp_decisions):
    tmp_decisions.parent.mkdir(parents=True)
    tmp_decisions.write_text('{"kind":"ref"}\nnot json\n')
    with pytest.raises(ValueError, match=":2"):
        D.read_all()


def test_target_key_matches_the_queue_row_id():
    assert D.target_key(D.append(dict(REF), reviewer="dan")) == REF["path"]
    assert D.target_key(D.append(dict(TERM), reviewer="dan")) == f"{TERM['node_id']}:0-21"
    assert D.target_key(D.append(dict(ANOM), reviewer="dan")) == f"{ANOM['node_id']}#0"


def test_the_latest_decision_wins():
    D.append(dict(REF, verdict="approve"), reviewer="dan")
    D.append({k: v for k, v in REF.items() if k != "chosen_candidate"} | {"verdict": "reject"},
             reviewer="sam")
    latest = D.decisions_by_target()[REF["path"]]
    assert latest["verdict"] == "reject" and latest["reviewer"] == "sam"


def test_summary_counts_by_kind_and_verdict():
    for d in (REF, TERM, ANOM):
        D.append(dict(d), reviewer="dan")
    s = D.summary()
    assert s["count"] == 3
    assert s["by_kind"] == {"ref": 1, "term": 1, "anomaly": 1}
    assert s["by_verdict"] == {"approve": 2, "reject": 1}
    assert s["recent"][0]["kind"] == "anomaly", "most recent first"
