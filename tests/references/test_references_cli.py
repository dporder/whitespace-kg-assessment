"""Stage 3 end to end: the CLI on the committed fixtures, and determinism."""
from __future__ import annotations

import json

import config
from pipeline.references.__main__ import main
from pipeline.schemas import RefsFile


def run(tmp_path, *extra):
    return main(["--input", "fixtures", "--run", "t", "--no-llm", "--quiet",
                 "--output-dir", str(tmp_path), *extra])


def refs_of(tmp_path, part) -> RefsFile:
    return RefsFile.model_validate(
        json.loads((tmp_path / "t" / "refs" / f"{part}.json").read_text()))


def test_the_cli_runs_clean_and_its_output_validates(tmp_path):
    assert run(tmp_path) == 0
    for part in ("core-terms", "joint-schedule-1", "award-form"):
        payload = refs_of(tmp_path, part)
        assert payload.part == part
        for ref in payload.refs:
            assert ref.kind == "ref" and ref.citable is False


def test_every_ref_span_reproduces_its_own_words(tmp_path):
    """The same check `tests/fixtures/test_fixtures_validate.py` makes."""
    run(tmp_path)
    trees = {}
    for part in ("core-terms", "joint-schedule-1", "award-form"):
        from pipeline.schemas import Node
        root = Node.model_validate(
            json.loads((config.ROOT / "fixtures" / "tree" / f"{part}.json").read_text()))
        stack = [root]
        while stack:
            node = stack.pop()
            trees[node.path] = node
            stack.extend(node.children)
    for part in ("core-terms", "joint-schedule-1"):
        for ref in refs_of(tmp_path, part).refs:
            parent = trees[ref.path.rsplit("/ref@", 1)[0]]
            start, end = ref.char_span
            assert (parent.text or "")[start:end] == ref.text


def test_the_ids_match_the_hand_made_fixture_refs(tmp_path):
    """Five of the seven committed refs come out identical, ids included; the
    other two are the bare Schedule 2, whose fixture shows the post-LLM state,
    and Schedule 6 (Materials), which needs stage 0's part register."""
    run(tmp_path)
    fixture = json.loads((config.ROOT / "fixtures" / "refs" / "core-terms.json").read_text())
    mine = {r.path: r for r in refs_of(tmp_path, "core-terms").refs}
    assert len(mine) == len(fixture["refs"])
    for row in fixture["refs"]:
        ref = mine[row["path"]]
        assert ref.id == row["id"]
        assert ref.lineage_key == row["lineage_key"]
        assert ref.text == row["text"]
        assert list(ref.char_span) == row["char_span"]
        assert ref.ref_kind == row["ref_kind"]


def test_detection_is_written_and_counted_separately(tmp_path):
    run(tmp_path)
    detection = json.loads(
        (tmp_path / "t" / "refs" / "detection" / "core-terms.json").read_text())
    assert detection["counts"]["pointers"] >= 6
    report = json.loads((tmp_path / "t" / "refs" / "report.json").read_text())
    assert report["detection"]["totals"]["pointers"] >= 6
    assert report["resolution"]["totals"]["by_status"]
    assert set(report["resolution"]["totals"]) >= {"by_status", "by_ref_kind",
                                                   "by_resolver", "by_scope_rule",
                                                   "by_kind_and_status"}


def test_the_deterministic_pass_is_byte_for_byte_stable(tmp_path):
    """SPEC ground rule 0: same input, same output, no clock, no ordering leak."""
    run(tmp_path)
    first = {p.name: p.read_bytes()
             for p in (tmp_path / "t" / "refs").glob("*.json")}
    run(tmp_path)
    second = {p.name: p.read_bytes()
              for p in (tmp_path / "t" / "refs").glob("*.json")}
    assert first.keys() == second.keys()
    for name in first:
        assert first[name] == second[name], f"{name} changed between identical runs"


def test_legislation_is_normalised_to_keys_and_reported(tmp_path):
    run(tmp_path)
    data = json.loads((tmp_path / "t" / "refs" / "legislation.json").read_text())
    assert data["records"][0]["key"] == "legislation/bribery-act-2010"
    assert data["by_instrument_kind"] == {"act": 1}
    assert data["near_miss_routing"]["embedding_arm"]["ran"] is False


def test_the_queue_files_say_what_is_waiting_on_what(tmp_path):
    run(tmp_path)
    queue = json.loads((tmp_path / "t" / "refs" / "llm_queue.json").read_text())
    assert queue["count"] >= 1
    assert "--no-llm" in queue["reason"]
    review = json.loads((tmp_path / "t" / "refs" / "review_queue.json").read_text())
    assert review["count"] >= 1
    assert all(item["status"] in ("ambiguous", "unresolved") for item in review["items"])


def test_a_batch_narrows_detection_but_not_the_corpus(tmp_path):
    """Resolution always runs against every tree the run holds, because that is
    what lets a ref flip when its target arrives."""
    assert run(tmp_path, "--batch", "B1") == 0
    assert (tmp_path / "t" / "refs" / "core-terms.json").exists()
    report = json.loads((tmp_path / "t" / "refs" / "report.json").read_text())
    assert report["input"]["parts_in_scope"] == ["core-terms"]
    assert len(report["input"]["parts_present"]) == 3


def test_reresolve_counts_the_transitions(tmp_path):
    run(tmp_path)
    assert run(tmp_path, "--reresolve") == 0
    report = json.loads((tmp_path / "t" / "refs" / "report.json").read_text())
    assert report["transitions"]["compared"] > 0
    assert report["transitions"]["changed"] == 0, "nothing changed, nothing moved"
