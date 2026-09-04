"""The deterministic typo-density signal."""
from __future__ import annotations

from collections import Counter

import config
from pipeline.vocabulary import treeio, typos
from tests.vocabulary.conftest import mk


def flag(token: str, previous=None, freq=None, buckets=None):
    return typos.flag_token(token, previous, freq or Counter(), buckets or {})


# ------------------------------------------------------------ the detectors


def test_a_stray_character_glued_to_a_capitalised_word_is_flagged():
    """The Award Form really prints `rFramework`. It is logged, never repaired."""
    assert flag("rFramework")[0] == "stray_leading_character"


def test_an_uppercase_letter_loose_inside_a_word_is_flagged():
    assert flag("SUpplier")[0] == "internal_capital"


def test_acronyms_and_their_plurals_are_not_typos():
    """`IPRs` and `SMEs` are all over this pack; flagging them would make every
    intellectual-property section read as typo dense."""
    for token in ("IPR", "IPRs", "CCS", "Supplier", "Call", "supplier", "O'Brien"):
        assert flag(token) is None, token


def test_a_repeated_word_is_flagged():
    assert flag("the", previous="the")[0] == "doubled_word"
    assert flag("Widget", previous="the") is None


def test_a_vowelless_word_is_flagged_but_an_acronym_is_not():
    assert flag("bcdfg")[0] == "no_vowel"
    assert flag("NDPB") is None


# ---------------------------------------------------- the corpus-based half


def test_a_rare_token_one_edit_from_a_common_one_is_flagged():
    freq = Counter({"supplier": 40, "suppler": 1})
    buckets = typos._near_common_index(freq)
    assert flag("suppler", freq=freq, buckets=buckets)[0] == "rare_near_common"


def test_inflections_of_one_stem_are_not_typos_of_each_other():
    """`provided` and `provider` are one edit apart and both ordinary English.
    Without this guard every contract section trips the detector."""
    freq = Counter({"provider": 40, "provided": 1})
    buckets = typos._near_common_index(freq)
    assert flag("provided", freq=freq, buckets=buckets) is None
    assert typos._inflection_pair("provided", "provider")
    assert not typos._inflection_pair("calloff", "callof")


def test_the_edit_detector_switches_itself_off_on_a_small_corpus():
    """Its premise is that ordinary vocabulary has recurred. On a fixture-sized
    corpus it has not, so the detector is disabled and the output says so
    rather than reporting a density it should not have measured."""
    node = mk("p/1/1.1", "clause", order=1, label="1.1",
              text="The provider provided the outputs.")
    part = mk("p", "part", order=0, title="Part", part_family="core", children=[node])
    trees = treeio.Trees(source="test", root=None, run="t", parts={"p": part}, files={})
    signal = typos.compute(trees, config.TYPO_DENSITY_THRESHOLD)
    assert signal.edit_check_ran is False
    assert signal.as_dict()["rare_near_common"]["ran"] is False
    assert "rare_near_common" not in signal.as_dict()["detectors"]


# ---------------------------------------------------------------- the signal


def test_density_is_flagged_tokens_over_word_tokens_and_uses_the_config_threshold():
    clean = mk("p/1/1.1", "clause", order=1, label="1.1",
               text="The Widget shall be supplied under the Contract in accordance "
                    "with the Schedule and the Order Form as agreed.")
    dirty = mk("p/2/2.1", "clause", order=2, label="2.1",
               text="The rFramework Contract shall bind the the SUpplier.")
    part = mk("p", "part", order=0, title="Part", part_family="core",
              children=[mk("p/1", "heading", order=1, label="1", title="Clean",
                           children=[clean]),
                        mk("p/2", "heading", order=2, label="2", title="Dirty",
                           children=[dirty])])
    trees = treeio.Trees(source="test", root=None, run="t", parts={"p": part}, files={})
    signal = typos.compute(trees, config.TYPO_DENSITY_THRESHOLD)
    assert signal.sections["p/1"].flagged == 0
    assert signal.sections["p/2"].flagged == 3       # rFramework, the the, SUpplier
    assert signal.is_typo_dense(dirty.id) is True
    assert signal.is_typo_dense(clean.id) is False
    assert [s.section_path for s in signal.dense_sections()] == ["p/2"]


def test_the_signal_is_deterministic():
    node = mk("p/1/1.1", "clause", order=1, label="1.1",
              text="The rFramework Contract binds the SUpplier and the Widget.")
    part = mk("p", "part", order=0, title="Part", part_family="core", children=[node])
    trees = treeio.Trees(source="test", root=None, run="t", parts={"p": part}, files={})
    first = typos.compute(trees, config.TYPO_DENSITY_THRESHOLD).as_dict()
    for _ in range(3):
        assert typos.compute(trees, config.TYPO_DENSITY_THRESHOLD).as_dict() == first
