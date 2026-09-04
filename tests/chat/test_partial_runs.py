"""Partial run directories, and the copy shown when data is simply absent.

Both came out of a live demo against a real graph: the run-directory picker
selected a cache folder with no trees and every page crop 404'd, and the trace
told the reader a defined term was "not defined" when its schedule merely had
not been loaded.
"""
import json
import shutil
from pathlib import Path

import pytest

from chat import config as ui_config
from chat.backends.fixtures import FixturesBackend
from chat.source import Corpus
from chat.tools import _summarise

FIXTURES = Path(ui_config.ROOT) / "fixtures"


@pytest.fixture
def run_dirs(tmp_path, monkeypatch):
    """An output/ shaped like the demo environment: one real run beside two decoys."""
    out = tmp_path / "output"
    (out / "llm_cache").mkdir(parents=True)
    (out / "llm_cache" / "a.json").write_text("{}")
    (out / "eval").mkdir()
    real = out / "run-b1"
    (real / "tree").mkdir(parents=True)
    for f in (FIXTURES / "tree").glob("*.json"):
        shutil.copy(f, real / "tree" / f.name)
    monkeypatch.setattr(ui_config.pipeline_config, "OUTPUT", out)
    monkeypatch.setattr(ui_config, "DATA_SOURCE", "output")
    monkeypatch.setattr(ui_config, "OUTPUT_RUN", None)
    return out, real


def test_a_directory_without_trees_is_not_a_run_directory(run_dirs):
    out, real = run_dirs
    assert ui_config.is_run_dir(real)
    assert not ui_config.is_run_dir(out / "llm_cache")
    assert not ui_config.is_run_dir(out / "eval")


def test_the_picker_skips_caches_even_when_they_sort_last(run_dirs):
    """`sorted(output/*)[-1]` chose a cache folder, which emptied the corpus."""
    out, real = run_dirs
    assert ui_config.data_root() == real


def test_an_explicit_pin_wins(run_dirs, monkeypatch):
    out, real = run_dirs
    monkeypatch.setattr(ui_config, "OUTPUT_RUN", "run-b1")
    assert ui_config.data_root() == real
    monkeypatch.setattr(ui_config, "OUTPUT_RUN", "nope")
    with pytest.raises(FileNotFoundError, match="does not exist"):
        ui_config.data_root()


def test_no_run_directory_names_what_it_did_find(run_dirs, monkeypatch):
    out, real = run_dirs
    shutil.rmtree(real)
    with pytest.raises(FileNotFoundError, match="llm_cache"):
        ui_config.data_root()


# --------------------------------------------------------------------------
# trees alone must be enough to serve a page crop
# --------------------------------------------------------------------------
def test_a_trees_only_run_still_serves_provisions_and_crops(run_dirs):
    out, real = run_dirs
    assert not (real / "refs").exists() and not (real / "vocab").exists()
    c = Corpus.load(real)
    assert c.trees and not c.refs and not c.definition_sites
    assert c.problems == []

    b = FixturesBackend(c)
    assert b.get_provision("core-terms/9/9.2")["found"] is True
    out_cite = b.cite("core-terms/9/9.2")
    assert out_cite["found"] is True
    assert out_cite["png"][:8] == b"\x89PNG\r\n\x1a\n"


def test_one_unreadable_artifact_does_not_take_the_corpus_down(run_dirs):
    """Trees landing before refs is the normal state of a running pipeline."""
    out, real = run_dirs
    (real / "refs").mkdir()
    (real / "refs" / "core-terms.json").write_text("{ this is not json")
    c = Corpus.load(real)
    assert c.trees, "a bad refs file must not cost us the trees"
    assert any("core-terms.json" in p for p in c.problems)
    assert FixturesBackend(c).cite("core-terms/9/9.2")["found"] is True


def test_the_report_shows_what_is_missing(run_dirs):
    out, real = run_dirs
    r = Corpus.load(real).report()
    assert r["trees"] == 3 and r["refs"] == 0
    assert r["vocabulary_loaded"] is False and r["refs_loaded"] is False
    assert r["data_root"] == str(real)


def test_a_crop_failure_says_which_kind_of_failure_it_is(run_dirs):
    out, real = run_dirs
    c = Corpus.load(real)
    missing = FixturesBackend(c).cite("core-terms/99/99.9")
    assert missing["found"] is False
    assert "no provision at that path" in missing["reason"]

    empty = Corpus(root=real)
    gone = FixturesBackend(empty).cite("core-terms/9/9.2")
    assert "no parsed trees" in gone["reason"], "an empty root must say so"


# --------------------------------------------------------------------------
# the copy a reader sees when data is absent
# --------------------------------------------------------------------------
def test_an_unloaded_vocabulary_is_not_reported_as_an_undefined_term(run_dirs):
    """The term is defined; its schedule is simply not in this slice."""
    out, real = run_dirs
    b = FixturesBackend(Corpus.load(real))
    res = b.define("Central Buying Office")
    assert res["found"] is False
    assert res["gap"] == "vocabulary_not_loaded"
    line = _summarise("define", res, True)
    assert "not defined" not in line
    assert "not loaded into this document set yet" in line


def test_a_genuinely_unknown_term_says_so():
    b = FixturesBackend()          # full fixtures, vocabulary present
    res = b.define("Force Majeure")
    assert res["gap"] == "term_not_defined"
    assert _summarise("define", res, True) == "no definition of that term in this document set"


def test_a_found_definition_names_where_it_lives():
    b = FixturesBackend()
    line = _summarise("define", b.define("Central Buying Office"), True)
    assert line.startswith("defined in Joint Schedule 1")


def test_missing_topics_do_not_leak_internal_vocabulary(run_dirs):
    out, real = run_dirs
    b = FixturesBackend(Corpus.load(real))
    res = b.find_by_concept("termination")
    assert res["gap"] == "concepts_not_loaded"
    line = _summarise("find_by_concept", res, True)
    assert "citable" not in line
    assert line == "no topic tags have been generated for this document set yet"


def test_the_trace_never_shows_internal_vocabulary():
    b = FixturesBackend()
    lines = [
        _summarise("find_provision", b.find_provision("IPR"), True),
        _summarise("define", b.define("Central Buying Office"), True),
        _summarise("find_by_concept", b.find_by_concept("intellectual property"), True),
        _summarise("get_provision", b.get_provision("core-terms/9/9.2"), True),
    ]
    banned = ("citable", "vector arm", "node", "corpus", "resolver", "backend")
    for line in lines:
        for word in banned:
            assert word not in line.lower(), f"{line!r} leaks {word!r}"


def test_the_search_line_explains_itself_in_plain_words():
    line = _summarise("find_provision", FixturesBackend().find_provision("IPR"), True)
    assert "keyword search only" in line and "semantic index not built yet" in line
