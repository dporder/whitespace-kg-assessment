"""Which node gets embedded at which altitude."""
from __future__ import annotations

import config
from pipeline.embeddings import plan as plan_mod
from pipeline.embeddings.tokens import estimate_tokens
from pipeline.vocabulary import treeio


def trees_of(*parts) -> treeio.Trees:
    return treeio.Trees(source="test", root=None, run="t",
                        parts={p.path: p for p in parts}, files={})


def items_by_path(plan) -> dict:
    return {i.path: i for i in plan.items}


# ----------------------------------------------------------------- altitudes


def test_leaves_get_their_own_text(small_tree):
    items = items_by_path(plan_mod.build(trees_of(small_tree)))
    leaf = items["p/1/1.2"]
    assert leaf.level == "leaf_text"
    assert leaf.text == small_tree.children[0].children[1].text
    assert items["p/1/1.1/a"].level == "leaf_text"


def test_a_container_within_budget_gets_its_subtree_text(small_tree):
    items = items_by_path(plan_mod.build(trees_of(small_tree)))
    container = items["p/1/1.1"]
    assert container.level == "subtree_text"
    assert "keep the register" in container.text
    assert "The Holder shall:" in container.text
    assert container.tokens <= config.SUBTREE_EMBED_TOKEN_BUDGET


def test_a_container_over_budget_gets_a_summary(long_container):
    items = items_by_path(plan_mod.build(trees_of(long_container)))
    container = items["q/1"]
    assert container.level == "summary"
    assert container.needs_summary is True
    assert container.tokens > config.SUBTREE_EMBED_TOKEN_BUDGET
    assert container.text == "", "no summary text is invented before one is generated"


def test_documents_and_parts_always_get_a_summary(small_tree):
    items = items_by_path(plan_mod.build(trees_of(small_tree)))
    assert items["p"].level == "summary"
    assert "always" in items["p"].reason


def test_the_budget_boundary_is_the_configured_number(small_tree):
    """Raise the budget and nothing changes; drop it below the subtree's size and
    the same container flips to a summary. The threshold lives in config, not in
    a constant buried in the code."""
    tight = estimate_tokens(treeio.subtree_text(small_tree.children[0])) - 1
    items = items_by_path(plan_mod.build(trees_of(small_tree), budget=tight))
    assert items["p/1"].level == "summary"
    items = items_by_path(plan_mod.build(trees_of(small_tree), budget=tight + 1))
    assert items["p/1"].level == "subtree_text"


# ------------------------------------------------------------- leaf_window


def test_leaf_window_is_off_by_default():
    assert config.LEAF_WINDOW_EMBEDDING is False


def test_leaf_window_replaces_leaf_text_and_never_doubles_it(small_tree):
    plan = plan_mod.build(trees_of(small_tree), leaf_window=True)
    levels = {i.level for i in plan.items}
    assert "leaf_window" in levels
    assert "leaf_text" not in levels
    per_node = {}
    for item in plan.items:
        per_node.setdefault(item.node_id, []).append(item.level)
    assert all(len(v) == 1 for v in per_node.values())


def test_the_window_carries_the_neighbouring_siblings(small_tree):
    items = items_by_path(plan_mod.build(trees_of(small_tree), leaf_window=True))
    window = items["p/1/1.1/a"]
    assert "keep the register" in window.text            # its own words
    assert "The Holder shall:" in window.text            # previous sibling
    assert "notify each change." in window.text          # next sibling


# ------------------------------------------------------------------ hygiene


def test_a_node_with_no_text_is_skipped_and_recorded(small_tree):
    empty = small_tree.children[0]
    empty.children[1].text = ""
    empty.children[1].content_hash = None
    plan = plan_mod.build(trees_of(small_tree))
    assert any(s["path"] == "p/1/1.2" for s in plan.skipped)
    assert "p/1/1.2" not in items_by_path(plan)


def test_the_plan_is_deterministic(small_tree, long_container):
    trees = trees_of(small_tree, long_container)
    first = [i.as_dict() for i in plan_mod.build(trees).items]
    for _ in range(3):
        assert [i.as_dict() for i in plan_mod.build(trees).items] == first


def test_every_node_appears_at_most_once(small_tree, long_container):
    plan = plan_mod.build(trees_of(small_tree, long_container))
    ids = [i.node_id for i in plan.items]
    assert len(ids) == len(set(ids))


def test_refs_are_never_embedded(small_tree):
    """A ref is a claim about a citation, not ink anyone retrieves on."""
    plan = plan_mod.build(trees_of(small_tree))
    assert all(i.kind != "ref" for i in plan.items)
