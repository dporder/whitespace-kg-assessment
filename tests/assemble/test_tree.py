"""Stage 2: the provision tree, the branch-or-leaf rule, identity and labels."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from pipeline.assemble.tree import build_part, renumber
from pipeline.schemas import ANATOMY_KINDS, Node, content_hash, lineage_key, node_id


def _tree(layout, profile) -> Node:
    root, _ = build_part(layout, profile)
    renumber(root)
    return Node.model_validate(root.model_dump())


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _by_path(root: Node) -> dict[str, Node]:
    return {n.path: n for n in _walk(root)}


@pytest.fixture(scope="module")
def core(core_terms_layout, profile):
    return _tree(core_terms_layout, profile)


@pytest.fixture(scope="module")
def award(award_form_layout, profile):
    return _tree(award_form_layout, profile)


@pytest.fixture(scope="module")
def definitions(definitions_layout, profile):
    return _tree(definitions_layout, profile)


def test_the_tree_validates_against_the_frozen_schema(core, award, definitions):
    for root in (core, award, definitions):
        Node.model_validate(root.model_dump())


def test_branch_or_leaf_holds_at_every_depth(core, award, definitions):
    """A node has anatomy children or it has text, never both, at any depth."""
    for root in (core, award, definitions):
        for node in _walk(root):
            anatomy = [c for c in node.children if c.kind in ANATOMY_KINDS]
            assert not (anatomy and node.text is not None), node.path


def test_a_lead_in_becomes_an_intro_child(core):
    """Clause 2.4 opens "If the Buyer decides to buy Deliverables ... the Buyer
    can:" and then lists (a) to (d). The lead-in is a child, not the parent's
    text."""
    nodes = _by_path(core)
    clause = nodes["core-terms/2/2.4"]
    assert clause.text is None
    intro = clause.children[0]
    assert intro.kind == "intro"
    assert intro.path == "core-terms/2/2.4/intro"
    assert intro.citable is False
    assert intro.text.startswith("If the Buyer decides to buy Deliverables")
    assert [c.label for c in clause.children[1:]] == ["(a)", "(b)", "(c)", "(d)"]


def test_a_bare_grouping_number_is_a_heading_with_no_intro(core):
    """3.1 groups 3.1.1 and 3.1.2 and has no sentence of its own, so it takes a
    title and gets neither text nor an intro child."""
    nodes = _by_path(core)
    for path, title in (
        ("core-terms/3/3.1", "All deliverables"),
        ("core-terms/3/3.2", "Goods clauses"),
    ):
        node = nodes[path]
        assert node.kind == "heading"
        assert node.title == title
        assert node.text is None
        assert not any(c.kind == "intro" for c in node.children)
        assert node.children, "a grouping number groups something"


def test_a_leaf_carries_its_own_words(core):
    node = _by_path(core)["core-terms/3/3.1/3.1.2"]
    assert node.kind == "subclause"
    assert not [c for c in node.children if c.kind in ANATOMY_KINDS]
    assert node.text.startswith("The Supplier must provide Deliverables with a warranty")
    assert node.content_hash == content_hash(node.text)


def test_identity_uses_the_schema_helpers(core):
    """Ids come from schemas.py with document rm6116 and the part's own
    template version, so nobody reimplements the id scheme."""
    node = _by_path(core)["core-terms/9/9.2"]
    assert core.template_version == "v3.0.11"
    assert node.id == node_id(config.DOCUMENT_ID, "v3.0.11", node.path)
    assert node.lineage_key == lineage_key(config.DOCUMENT_ID, node.path)


def test_unit_labels_and_their_source(core, definitions):
    """A provision of the Core Terms is a Clause and the same shape inside a
    schedule is a Paragraph, both from the interpretation clause. Lettered
    items are what the interpretation clause is silent on, so their label comes
    from the profile and says so."""
    nodes = _by_path(core)
    clause = nodes["core-terms/9/9.2"]
    assert (clause.unit_label, clause.unit_label_source) == ("Clause", "document")
    item = nodes["core-terms/2/2.4/a"]
    assert (item.unit_label, item.unit_label_source) == ("Paragraph", "profile")
    assert definitions.unit_label == "Paragraph"
    assert definitions.unit_label_source == "document"


def test_pages_boxes_and_printed_pages_are_carried(core):
    node = _by_path(core)["core-terms/3/3.1/3.1.2"]
    assert node.page_start == node.page_end == 3
    assert node.printed_page == "3"
    assert [b.page for b in node.bboxes_own] == [3]
    assert [b.page for b in node.bboxes_extent] == [3]


def test_extents_cover_one_entry_per_page_touched(core):
    for node in _walk(core):
        pages = [b.page for b in node.bboxes_extent]
        assert pages == sorted(set(pages)), node.path
        if pages:
            assert node.page_start == min(pages) and node.page_end == max(pages), node.path


def test_order_is_preorder_within_the_part(core):
    orders = [n.order for n in _walk(core)]
    assert orders == list(range(len(orders)))
    assert core.order == 0


def test_paths_are_unique(core, award, definitions):
    for root in (core, award, definitions):
        paths = [n.path for n in _walk(root)]
        assert len(paths) == len(set(paths)), f"duplicate paths in {root.path}"


def test_form_rows_hold_label_and_value_cells(award):
    nodes = _by_path(award)
    row = nodes["award-form/3"]
    assert row.kind == "form_row"
    assert row.label == "3"
    assert row.text is None
    label = nodes["award-form/3/label"]
    value = nodes["award-form/3/value"]
    assert (label.kind, label.cell_role) == ("cell", "label")
    assert (value.kind, value.cell_role) == ("cell", "value")
    assert label.text == "rFramework Contract"
    assert any(a.startswith("stray_character_in_label") for a in label.anomalies)
    assert label.role_confidence is not None


def test_placeholders_are_preserved_verbatim(award):
    texts = [n.text for n in _walk(award) if n.text]
    assert any(t.startswith("[Insert name (registered name if registered)]") for t in texts) or any(
        "[Insert name (registered name if registered)]" in t for t in texts
    )


def test_definitions_become_a_table_of_cells(definitions):
    tables = [n for n in _walk(definitions) if n.kind == "table"]
    assert tables
    table = tables[0]
    assert table.n_cols == 2 and table.n_rows > 1
    assert table.text is None
    assert all(c.kind == "cell" for c in table.children)
    terms = [c.text for c in table.children if c.col == 0]
    assert "Accounting Reference Date" in terms


def test_a_container_that_gains_children_keeps_its_number_as_its_own_ink(core):
    """Once a lead-in moves to an intro child, the container's own box is its
    printed number, which is what the left-edge invariant compares against."""
    clause = _by_path(core)["core-terms/2/2.4"]
    assert clause.bboxes_own, "the container keeps ink of its own"
    box = clause.bboxes_own[0].bbox
    assert box[2] - box[0] < 40.0, "own box is the number, not the whole line"
    assert box[0] <= clause.children[0].bboxes_own[0].bbox[0]


def test_a_wrapped_line_starting_with_a_citation_is_not_a_new_clause(core):
    """"... any breach of Clauses 27.1 or 27.2 or has any reason to think ..."
    wraps onto a line starting "27.1". There is one 27.1, not two."""
    nodes = _by_path(core)
    assert "core-terms/27/27.3" in nodes
    # 27.3 has lettered children, so its lead-in lives on its intro child.
    text = nodes["core-terms/27/27.3/intro"].text
    assert "27.1 or 27.2" in text
    clause_27 = nodes["core-terms/27"]
    labels = [c.label for c in clause_27.children if c.label]
    assert labels == sorted(set(labels), key=labels.index)
    assert labels.count("27.1") == 1


def test_headings_without_numbered_children_keep_their_prose(core):
    """Clause 35 is a heading whose whole body is one unnumbered sentence."""
    node = _by_path(core)["core-terms/35"]
    assert node.kind == "heading"
    assert node.title == "Which law applies"
    assert node.text is not None
    assert node.text.startswith("This Contract and any Disputes")
    assert not [c for c in node.children if c.kind in ANATOMY_KINDS]
