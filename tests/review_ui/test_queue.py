"""The review queue: what lands in it, and what each row carries.

A row exists so a reviewer can judge without leaving the page, so every row is
checked for the three things SPEC 6 requires: the source sentence with the span
that triggered it, a crop resolving to a page image, and the candidates.
"""
import pytest

import review_data as R
import review_decisions as D
from chat.source import corpus


@pytest.fixture(autouse=True)
def tmp_decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "path", lambda: tmp_path / "golden" / "decisions.jsonl")


@pytest.fixture(scope="module")
def c():
    return corpus()


@pytest.fixture
def rows():
    return R.queue()


# ------------------------------------------------------------- what lands
def test_only_ambiguous_and_unresolved_refs_land(rows, c):
    queued = {r["path"] for r in rows if r["kind"] == "ref"}
    expected = {r.path for r in c.refs if r.status in ("ambiguous", "unresolved")}
    assert queued == expected
    resolved = {r.path for r in c.refs if r.status in ("resolved", "external")}
    assert not (queued & resolved), "a settled ref must not ask for review"


def test_only_ambiguous_term_uses_land(rows, c):
    queued = {r["id"] for r in rows if r["kind"] == "term"}
    expected = {f"{u.node_id}:{u.char_span[0]}-{u.char_span[1]}"
                for u in c.term_uses if u.status == "ambiguous"}
    assert queued == expected


def test_every_anomaly_lands(rows, c):
    assert sum(1 for r in rows if r["kind"] == "anomaly") == sum(
        len(n.anomalies) for n in c.by_path.values()
    )


def test_all_three_kinds_are_present(rows):
    assert {r["kind"] for r in rows} == {"ref", "term", "anomaly"}


# ---------------------------------------------------------- what a row has
def test_every_row_carries_a_sentence_a_crop_and_an_identity(rows):
    for r in rows:
        assert r["id"] and r["kind"] and r["part"] and r["path"]
        assert isinstance(r["page"], int)
        assert r["sentence"]["text"], f"{r['id']} has no source sentence"
        assert r["crop"] is not None, f"{r['id']} has no crop"
        assert r["crop"]["url"].startswith("/api/crop?")
        assert len(r["crop"]["bbox"]) == 4


def test_a_span_reproduces_its_surface_text(rows, c):
    """The highlight must land on the words that triggered the row."""
    for r in rows:
        span = r["sentence"]["span"]
        if not span:
            continue
        a, b = span
        surface = r["sentence"]["text"][a:b]
        if r["kind"] == "ref":
            assert surface == r["detail"]["pointing_words"]
        else:
            assert surface == r["detail"]["term"]


def test_an_ambiguous_ref_shows_its_candidates(rows):
    row = next(r for r in rows if r["path"] == "core-terms/9/9.2/ref@111-121")
    assert row["status"] == "ambiguous"
    assert [c["path"] for c in row["candidates"]] == [
        "framework-schedule-2", "call-off-schedule-2"
    ]
    assert row["detail"]["target_path"] is None
    assert row["detail"]["resolver"] == "llm"


def test_an_unresolved_ref_keeps_the_candidate_it_could_not_confirm(rows):
    row = next(r for r in rows if r["path"] == "core-terms/9/9.1/intro/ref@28-71")
    assert row["status"] == "unresolved"
    assert row["candidates"][0]["path"] == "framework-schedule-4"
    assert row["label"] == "Clause 9.1 lead-in"


def test_a_term_row_shows_the_governing_definition(rows):
    row = next(r for r in rows if r["kind"] == "term")
    assert row["detail"]["term"] == "Good Working Practice"
    assert row["detail"]["ambiguity_kind"] == "sentence_initial"
    assert row["detail"]["governing_scope"] == "document"
    assert row["detail"]["governing_path"] == "joint-schedule-1/2/table/3/1"
    assert row["detail"]["governing_text"].startswith("standards which a skilled person")


def test_an_anomaly_row_keeps_the_raw_text_and_flags_the_reading(rows):
    row = next(r for r in rows if r["path"] == "award-form/3/label")
    assert row["sentence"]["text"] == "rFramework Contract", "raw text is never repaired"
    assert row["detail"]["found_token"] == "rFramework"
    assert row["detail"]["proposed_token"] == "Framework"
    # the reading the reviewer judges is the whole text with that one swap
    assert row["detail"]["proposed"] == "Framework Contract"
    assert row["detail"]["code"] == "stray_character_in_label"


def test_a_proposed_reading_is_never_offered_unless_the_token_is_really_there():
    """No substitution is invented when the anomaly does not match the text."""
    out = R.parse_anomaly("stray_character_in_label: 'zzz' for 'Zzz'", anchor="rFramework Contract")
    assert out["proposed_token"] == "Zzz"
    assert out["proposed"] is None


def test_an_anomaly_without_a_proposed_reading_says_so(rows):
    row = next(r for r in rows if r["path"] == "core-terms/9/9.2" and r["kind"] == "anomaly")
    assert row["detail"]["code"] == "numbering_gap_after_9.2"
    assert row["detail"]["proposed"] is None


def test_crop_colour_follows_the_trust_gradient(rows):
    by_kind = {r["kind"]: r["crop"]["colour"] for r in rows}
    assert by_kind["ref"] == "deterministic"
    assert by_kind["term"] == "rule"
    assert by_kind["anomaly"] == "ink"


# ------------------------------------------------------------- filtering
def test_filtering_by_kind(rows):
    only = R.queue(kinds=("ref",))
    assert {r["kind"] for r in only} == {"ref"}
    assert len(only) == sum(1 for r in rows if r["kind"] == "ref")


def test_filtering_by_part(rows):
    only = R.queue(part="award-form")
    assert only and all(r["part"] == "award-form" for r in only)


def test_refs_lead_the_queue(rows):
    kinds = [r["kind"] for r in rows]
    assert kinds == sorted(kinds, key=lambda k: {"ref": 0, "term": 1, "anomaly": 2}[k])


def test_counts_add_up(rows):
    counts = R.counts(rows)
    assert counts["total"] == len(rows) == counts["ref"] + counts["term"] + counts["anomaly"]
    assert sum(counts["parts"].values()) == counts["total"]


# --------------------------------------------------- decisions on the rows
def test_a_row_shows_the_verdict_recorded_against_it():
    ref_path = "core-terms/9/9.2/ref@111-121"
    assert next(r for r in R.queue() if r["path"] == ref_path)["decided"] is None

    D.append({"kind": "ref", "path": ref_path, "verdict": "approve",
              "chosen_candidate": "framework-schedule-2"}, reviewer="dan")

    row = next(r for r in R.queue() if r["path"] == ref_path)
    assert row["decided"]["verdict"] == "approve"
    assert row["decided"]["chosen_candidate"] == "framework-schedule-2"
    assert R.counts(R.queue())["decided"] == 1


def test_decided_rows_can_be_hidden():
    ref_path = "core-terms/9/9.2/ref@111-121"
    D.append({"kind": "ref", "path": ref_path, "verdict": "reject"}, reviewer="dan")
    remaining = {r["path"] for r in R.queue(include_decided=False)}
    assert ref_path not in remaining


def test_a_term_verdict_matches_its_row_id():
    row = next(r for r in R.queue() if r["kind"] == "term")
    D.append({"kind": "term", "node_id": row["node_id"], "char_span": row["char_span"],
              "verdict": "reject"}, reviewer="dan")
    assert next(r for r in R.queue() if r["id"] == row["id"])["decided"]["verdict"] == "reject"


def test_an_anomaly_verdict_matches_its_row_id():
    row = next(r for r in R.queue() if r["kind"] == "anomaly")
    D.append({"kind": "anomaly", "node_id": row["node_id"], "anomaly": row["anomaly"],
              "anomaly_index": row["anomaly_index"], "verdict": "approve"}, reviewer="dan")
    assert next(r for r in R.queue() if r["id"] == row["id"])["decided"]["verdict"] == "approve"
