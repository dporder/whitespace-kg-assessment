"""The stratified audit sample: drawn as configured, and pinned to stage 8's."""
from __future__ import annotations

import config
from pipeline.vocabulary import audit
from pipeline.vocabulary.matching import Match


def make_matches(n: int, parts=("core-terms", "joint-schedule-1")) -> list[Match]:
    out = []
    for i in range(n):
        term = ["Widget", "Widget Register", "Central Buying Office"][i % 3]
        out.append(Match(
            term=term, surface=term, node_id=f"node-{i}", node_path=f"p/{i}",
            part=parts[i % len(parts)], section_path=f"p/{i // 10}",
            field_name="text", span=(0, len(term)),
            status="confident" if i % 5 else "ambiguous",
            ambiguity_kind="none" if i % 5 else "sentence_initial",
            order=i, sentence=f"sentence {i}"))
    return out


def orders_for(matches) -> dict[str, tuple[int, int]]:
    total = max(m.order for m in matches)
    return {m.node_id: (m.order, total) for m in matches}


# ------------------------------------------------------------ the contract


def test_the_sample_uses_the_configured_size_and_strata():
    matches = make_matches(300)
    drawn = audit.draw(matches, orders_for(matches), config.AUDIT, "run-1")
    assert drawn["config"]["confident_term_sample_size"] == \
        config.AUDIT["confident_term_sample_size"]
    assert drawn["config"]["strata"] == config.AUDIT["strata"]
    assert drawn["sample"]["drawn_sample_size"] == \
        config.AUDIT["confident_term_sample_size"]
    assert len(drawn["items"]) == config.AUDIT["confident_term_sample_size"]


def test_only_confident_matches_are_audited():
    """Layer 4 exists to catch a systematic error in the *easy* cases; the hard
    ones already went to their own routed check."""
    matches = make_matches(300)
    drawn = audit.draw(matches, orders_for(matches), config.AUDIT, "run-1")
    confident_ids = {m.node_id for m in matches if m.status == "confident"}
    assert all(item["node_id"] in confident_ids for item in drawn["items"])
    assert drawn["sample"]["population_size"] == len(confident_ids)


def test_the_draw_is_deterministic_and_seeded_from_the_population():
    matches = make_matches(300)
    first = audit.draw(matches, orders_for(matches), config.AUDIT, "run-1")
    assert audit.draw(matches, orders_for(matches), config.AUDIT, "run-1") == first
    other = audit.draw(matches, orders_for(matches), config.AUDIT, "run-2")
    assert other["sample"]["seed"] != first["sample"]["seed"]


def test_a_population_smaller_than_the_sample_is_taken_whole_and_says_so():
    matches = make_matches(12)
    drawn = audit.draw(matches, orders_for(matches), config.AUDIT, "run-1")
    assert drawn["sample"]["drawn_sample_size"] == drawn["sample"]["population_size"]
    assert drawn["sample"]["requested_sample_size"] == 40


def test_every_stratum_present_in_the_population_is_represented_proportionally():
    matches = make_matches(300)
    drawn = audit.draw(matches, orders_for(matches), config.AUDIT, "run-1")
    cells = drawn["sample"]["cells"]
    assert sum(c["sampled"] for c in cells) == drawn["sample"]["drawn_sample_size"]
    assert sum(c["population"] for c in cells) == drawn["sample"]["population_size"]
    biggest = max(cells, key=lambda c: c["population"])
    assert biggest["sampled"] >= 1


# ------------------------------------------------------------- anti-drift


def test_the_sampler_agrees_exactly_with_the_eval_harness():
    """Stage 4 draws its own sample rather than importing stage 8's, so that an
    enrichment stage does not depend on the evaluation stage. This test is what
    stops the two implementations drifting apart in silence: if eval-builder
    changes the algorithm, this fails instead of the two quietly auditing
    different samples."""
    from pipeline.eval import sampling as eval_sampling

    matches = make_matches(300)
    population = audit.population(matches, orders_for(matches))
    strata = config.AUDIT["strata"]
    size = config.AUDIT["confident_term_sample_size"]
    seed_material = f"{audit.POPULATION_NAME}|run-1"

    stratifier = audit.stratifier(strata)
    mine = audit.stratified_sample(population, stratifier, size, strata,
                                   seed_material=seed_material)
    theirs = eval_sampling.stratified_sample(population, stratifier, size, strata,
                                             seed_material=seed_material)
    assert mine.indices == theirs.indices
    assert mine.seed == theirs.seed
    assert mine.as_dict() == theirs.as_dict()


def test_the_stratum_buckets_agree_with_the_eval_harness():
    from pipeline.eval import sampling as eval_sampling
    from pipeline.vocabulary.text import position_bucket, word_count_bucket

    for text in ("Widget", "Widget Register", "Central Buying Office", ""):
        assert word_count_bucket(text) == eval_sampling.word_count_bucket(text)
    for order, total in ((0, 1), (0, 100), (40, 100), (80, 100), (99, 100)):
        assert position_bucket(order, total) == \
            eval_sampling.position_bucket(order, total)
