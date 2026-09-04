"""The words a reviewer actually reads.

Roughly 180 lines of review_data.py exist only to compose plain English, and
that copy is the deliverable as much as the layout is: Dan could not tell what
he was being asked to decide, and this is the layer that fixes it. So it is
pinned here — including the exact strings for the row that prompted the
rewrite, and the rule that every button a reviewer can press carries a verdict
the eval harness recognises.
"""
import re

import pytest

import review_data as R
import review_decisions as D
from chat.source import corpus


@pytest.fixture(autouse=True)
def tmp_decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "path", lambda: tmp_path / "golden" / "decisions.jsonl")


@pytest.fixture(scope="module")
def rows():
    return R.queue()


@pytest.fixture(scope="module")
def c():
    return corpus()


# --------------------------------------------------------------------------
# every button must be a verdict the harness accepts
# --------------------------------------------------------------------------
def test_every_answer_carries_a_verdict_in_the_golden_vocabulary(rows):
    """A typo'd verdict must fail here, not on a reviewer's click."""
    for r in rows:
        allowed = set(D.VERDICTS[r["kind"]])
        for a in r["copy"]["answers"]:
            assert a["verdict"] in allowed, (
                f"{r['id']} offers {a['verdict']!r}, which is not one of {sorted(allowed)}")


def test_answers_supply_chosen_candidate_exactly_where_it_is_required(rows):
    for r in rows:
        for a in r["copy"]["answers"]:
            needs = (r["kind"], a["verdict"]) in D.NEEDS_CANDIDATE
            has = bool(a.get("chosen_candidate"))
            assert needs == has, (
                f"{r['id']} / {a['verdict']}: chosen_candidate "
                f"{'missing' if needs else 'supplied but meaningless'}")


def test_every_answer_a_row_offers_would_validate(rows):
    """Compose the record each button would send and put it through validate."""
    for r in rows:
        for a in r["copy"]["answers"]:
            d = {"kind": r["kind"], "verdict": a["verdict"], "reviewer": "tester",
                 "ts": D.now()}
            if a.get("chosen_candidate"):
                d["chosen_candidate"] = a["chosen_candidate"]
            if r["kind"] == "ref":
                d["path"] = r["path"]
            elif r["kind"] == "term":
                d["node_id"], d["char_span"] = r["node_id"], r["char_span"]
            else:
                d["node_id"], d["anomaly"] = r["node_id"], r["anomaly"]
                d["anomaly_index"] = r["anomaly_index"]
            D.validate(d)          # raises if the button would be refused


def test_every_verdict_in_the_vocabulary_a_row_can_answer_is_reachable(rows):
    """Refs must expose all three; nothing is left to a keyboard-only path."""
    ref_verdicts = {a["verdict"] for r in rows if r["kind"] == "ref"
                    for a in r["copy"]["answers"]}
    assert ref_verdicts == set(D.VERDICTS["ref"])
    term_verdicts = {a["verdict"] for r in rows if r["kind"] == "term"
                     for a in r["copy"]["answers"]}
    assert term_verdicts == {"use", "not_a_use"}
    anomaly_verdicts = {a["verdict"] for r in rows if r["kind"] == "anomaly"
                        for a in r["copy"]["answers"]}
    assert anomaly_verdicts == {"confirmed", "rejected"}


# --------------------------------------------------------------------------
# every row says what it is asking
# --------------------------------------------------------------------------
def test_every_row_states_the_situation_the_reason_and_the_question(rows):
    for r in rows:
        cp = r["copy"]
        for key in ("situation", "explain", "question"):
            assert cp.get(key), f"{r['id']} has no {key}"
            assert len(cp[key].split()) >= 4, f"{r['id']} {key} is too terse to help"
        assert cp["question"].endswith("?"), f"{r['id']} question is not a question"


def test_every_answer_reads_as_an_answer_not_a_verdict_name(rows):
    machine = set(D.VERDICTS["ref"]) | set(D.VERDICTS["term"]) | set(D.VERDICTS["anomaly"])
    for r in rows:
        for a in r["copy"]["answers"]:
            assert a["label"], f"{r['id']} has an unlabelled answer"
            assert a["label"] not in machine, f"{r['id']} shows the raw verdict {a['label']!r}"
            assert "_" not in a["label"], f"{r['id']} leaks a machine name: {a['label']!r}"


def test_no_row_shows_a_path_or_a_bare_score_in_its_reader_facing_copy(rows):
    """Paths and scores belong in the technical disclosure, not the question."""
    for r in rows:
        cp = r["copy"]
        text = " ".join([cp["situation"], cp["explain"], cp["question"]]
                        + [a["label"] + " " + (a.get("sublabel") or "") for a in cp["answers"]])
        assert "/" not in text.replace("Call-Off", ""), f"{r['id']} shows a path: {text!r}"
        assert not re.search(r"\b0\.\d+\b", text), f"{r['id']} shows a bare score: {text!r}"


def test_every_row_is_named_the_way_the_agreement_names_it(rows):
    for r in rows:
        assert r["citation"], f"{r['id']} has no human citation"
        assert "/" not in r["citation"].replace("Call-Off", "")
        assert r["citation"] != r["path"]


# --------------------------------------------------------------------------
# guidelines
# --------------------------------------------------------------------------
def test_each_row_kind_has_guidelines_a_newcomer_could_follow(rows):
    kinds = {r["kind"] for r in rows}
    assert kinds <= set(R.GUIDELINES)
    for kind in kinds:
        g = R.GUIDELINES[kind]
        assert g["title"] and g["summary"] and g["why"]
        assert len(g["how"]) >= 3, f"{kind} guidance is too thin to act on"
        for step in g["how"]:
            assert step.endswith("."), f"{kind} guidance step is not a sentence: {step!r}"


# --------------------------------------------------------------------------
# the exact row that prompted the rewrite
# --------------------------------------------------------------------------
def test_the_ambiguous_schedule_2_row_composes_the_expected_words(rows):
    r = next(x for x in rows if x["id"] == "core-terms/9/9.2/ref@111-121")
    cp = r["copy"]
    assert r["citation"] == "Core Terms, Clause 9.2"
    assert cp["situation"] == "This sentence points at “Schedule 2”."
    assert cp["explain"] == ("This document set contains more than one thing called "
                             "“Schedule 2”.")
    assert cp["question"] == "Which one does the writer mean?"
    assert cp["confidence_words"] == "The system found nothing to prefer one over the other."
    assert [(a["label"], a["sublabel"]) for a in cp["answers"]] == [
        ("Framework Schedule 2", "part of the framework agreement itself"),
        ("Call-Off Schedule 2",
         "part of an individual contract called off under the framework"),
        ("None of these",
         "it points at something this document set does not contain"),
        ("It is not a cross-reference", "“Schedule 2” is ordinary wording here"),
    ]


def test_an_unloaded_target_asks_for_confirmation_not_an_apology(rows):
    """This row read as an error report. The reviewer's job is to confirm the
    detection and the intended target so the link connects when it arrives."""
    r = next(x for x in rows if x["id"] == "core-terms/9/9.1/intro/ref@28-71")
    cp = r["copy"]
    assert r["citation"] == "Core Terms, Clause 9.1, opening words"
    assert cp["question"] == "Does this really point at Framework Schedule 4?"
    assert cp["note"] == ("Confirming now means the link connects automatically "
                          "when Framework Schedule 4 arrives.")
    assert [a["label"] for a in cp["answers"]] == [
        "Yes, that is the target", "It points somewhere else", "It is not a cross-reference"]
    # nothing in it should read as a failure
    blob = " ".join([cp["situation"], cp["explain"], cp["question"], cp["note"]]).lower()
    for word in ("error", "failed", "could not be followed", "problem"):
        assert word not in blob, f"the row still reads as a failure: {word!r}"


def test_a_sentence_initial_term_explains_why_the_capital_is_doubtful(rows):
    r = next(x for x in rows if x["kind"] == "term")
    assert r["copy"]["situation"] == ("“Good Working Practice” is a term this "
                                      "agreement defines.")
    assert "start of a sentence" in r["copy"]["explain"]
    assert r["copy"]["question"] == "Is this the defined term, or ordinary words?"


def test_a_term_row_names_where_its_governing_definition_lives(rows):
    """The page prints this; without it the row read 'from  — applies ...'."""
    for r in [x for x in rows if x["kind"] == "term"]:
        d = r["detail"]
        if not d.get("governing_text"):
            continue
        assert d.get("governing_citation"), f"{r['id']} has definition text but no citation"
        assert "/" not in d["governing_citation"]
        assert d["governing_citation"].startswith("Joint Schedule 1")


def test_an_anomaly_offers_the_reading_it_would_record(rows):
    r = next(x for x in rows if x["path"] == "award-form/3/label")
    assert r["copy"]["situation"] == "The text here reads “rFramework Contract”."
    assert "kept exactly as printed" in r["copy"]["explain"]
    assert r["copy"]["question"] == "Should this be read as “Framework Contract”?"


def test_an_anomaly_with_no_proposed_reading_asks_a_different_question(rows):
    r = next(x for x in rows if x["kind"] == "anomaly" and not x["detail"]["proposed"])
    assert r["copy"]["question"] == "Is this fairly flagged?"
    assert "may be how the document is drafted" in r["copy"]["explain"]


# --------------------------------------------------------------------------
# naming and family words
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path, expected",
    [
        ("core-terms/9/9.2", "Core Terms, Clause 9.2"),
        ("core-terms/3/3.1/3.1.1/a", "Core Terms, Clause 3.1.1, paragraph (a)"),
        ("core-terms/9/9.1/intro", "Core Terms, Clause 9.1, opening words"),
        ("award-form/3/label", "Framework Award Form, row 3, the label"),
        ("joint-schedule-1/2/table/2/1",
         'Joint Schedule 1 (Definitions), the definition of "Central Buying Office" ("CBO")'),
    ],
)
def test_human_citations(c, path, expected):
    assert R.human_citation(c, c.by_path[path]) == expected


@pytest.mark.parametrize(
    "path, words",
    [
        ("framework-schedule-2", "part of the framework agreement itself"),
        ("call-off-schedule-9", "part of an individual contract called off under the framework"),
        ("joint-schedule-1",
         "shared by the framework agreement and the contracts called off under it"),
    ],
)
def test_candidate_families_are_explained_in_contract_terms(path, words):
    assert R.family_words(path)[1] == words


def test_the_page_renders_its_buttons_from_answers(rows):
    """The page must not carry its own copy of the verdict labels: if it did,
    these tests would pass while the screen showed something else."""
    from pathlib import Path

    page = (Path(R.__file__).parent / "static" / "index.html").read_text()
    assert "cp.answers" in page, "the page does not build its buttons from the composed answers"
    assert "a.verdict" in page and "a.label" in page

    # The wording lives on the server. A label appearing as a literal in the
    # page would mean the screen could say something these tests never see.
    # (`unresolvable` and `use` do appear, but only to pick a style and to
    # substitute the chosen term — never to supply a label.)
    labels = {a["label"] for r in rows for a in r["copy"]["answers"]}
    for label in labels:
        if label.startswith("Yes, it means"):
            continue                      # composed in the page from the term picker
        assert label not in page, f"the page hardcodes the answer label {label!r}"


def test_a_typod_verdict_is_caught_by_a_test_not_by_a_reviewer(monkeypatch):
    """The bar the reviewer set, demonstrated: break the vocabulary and the
    suite fails. This is the whole reason the copy layer is tested."""
    real = R.ref_copy

    def typo(c, ref, cands):
        out = real(c, ref, cands)
        out["answers"][-1]["verdict"] = "not_a_referenc"      # one character short
        return out

    monkeypatch.setattr(R, "ref_copy", typo)
    broken = R.queue()
    with pytest.raises(AssertionError):
        test_every_answer_carries_a_verdict_in_the_golden_vocabulary(broken)


def test_confidence_is_words_or_nothing():
    equal = [{"name": "A", "score": 0.5}, {"name": "B", "score": 0.5}]
    assert R._confidence_words(equal) == "The system found nothing to prefer one over the other."
    leaning = [{"name": "A", "score": 0.9}, {"name": "B", "score": 0.4}]
    assert R._confidence_words(leaning) == ("The system leaned towards A, but not enough "
                                            "to be sure.")
    assert R._confidence_words([{"name": "A", "score": 0.9}]) is None, "one candidate, no comparison"
