"""Unit tests for the invariant checks, on trees built to isolate one rule each."""
from __future__ import annotations

import pytest

import config
from pipeline.eval.sections import invariants
from pipeline.eval.sections.invariants import (CHECKS, AnomalyLedger, anomaly_key,
                                               check_tree, label_sequence_value,
                                               numbering_mode)
from pipeline.schemas import BBox, Node, content_hash, lineage_key, node_id

DOC, VERSION = "test-doc", "v1"


def mk(path, kind, order, box=None, page=1, **kw):
    text = kw.get("text")
    if box is not None:
        kw.setdefault("bboxes_own", [BBox(page=page, bbox=box)])
    return Node(id=node_id(DOC, VERSION, path), lineage_key=lineage_key(DOC, path),
                content_hash=content_hash(text) if text else None,
                path=path, kind=kind, page_start=page, page_end=kw.pop("page_end", page),
                order=order, **kw)


def extents(node: Node) -> dict[int, list[float]]:
    boxes: dict[int, list[float]] = {}

    def merge(page, b):
        if page in boxes:
            x0, y0, x1, y1 = boxes[page]
            boxes[page] = [min(x0, b[0]), min(y0, b[1]), max(x1, b[2]), max(y1, b[3])]
        else:
            boxes[page] = list(b)

    for bb in node.bboxes_own:
        merge(bb.page, bb.bbox)
    for child in node.children:
        for page, b in extents(child).items():
            merge(page, b)
    node.bboxes_extent = [BBox(page=p, bbox=tuple(b)) for p, b in sorted(boxes.items())]
    return boxes


def clean_pair(child_box=(110, 130, 400, 145), parent_box=(100, 100, 400, 115),
               **child_kw):
    """A parent heading with one leaf child, geometry clean by default."""
    child = mk("p/1/a", "item", 2, child_box, label="(a)", text="body", **child_kw)
    parent = mk("p/1", "heading", 1, parent_box, label="1", title="One", children=[child])
    root = mk("p", "part", 0, None, title="P", children=[parent])
    extents(root)
    return root


def failures(root: Node, check: str):
    violations, _checked, _skipped = check_tree("p", root)
    return [v for v in violations if v.check == check]


def test_clean_tree_has_no_violations():
    violations, checked, _ = check_tree("p", clean_pair())
    assert violations == []
    assert checked["child_left_edge"] >= 1


def test_child_left_of_parent_is_a_violation():
    root = clean_pair(child_box=(90, 130, 400, 145))
    hits = failures(root, "child_left_edge")
    assert len(hits) == 1 and hits[0].path == "p/1/a"
    assert hits[0].explained_by is None


def test_a_recorded_anomaly_explains_the_violation():
    root = clean_pair(child_box=(90, 130, 400, 145),
                      anomalies=["child_left_edge_outdent: set left in the source"])
    hits = failures(root, "child_left_edge")
    assert len(hits) == 1
    assert hits[0].explained_by.startswith("child_left_edge_outdent")


def test_own_box_below_first_child_is_a_violation():
    root = clean_pair(child_box=(110, 80, 400, 95))       # child above the parent
    assert [v.path for v in failures(root, "own_box_above_first_child")] == ["p/1"]


def test_siblings_side_by_side_on_one_line_are_fine():
    """A form row's label and value cells share a y band and must not be flagged."""
    label = mk("p/1/label", "cell", 2, (72, 130, 180, 145), text="Provider",
               row=0, col=0, cell_role="label")
    value = mk("p/1/value", "cell", 3, (200, 130, 470, 145), text="[Insert name]",
               row=0, col=1, cell_role="value")
    row = mk("p/1", "form_row", 1, None, label="1", children=[label, value])
    root = mk("p", "part", 0, None, title="P", children=[row])
    extents(root)
    assert failures(root, "siblings_ascend") == []


def test_table_cells_in_row_major_order_are_fine():
    cells = [
        mk("p/t/0/0", "cell", 2, (72, 140, 200, 155), text="a", row=0, col=0,
           cell_role="label"),
        mk("p/t/0/1", "cell", 3, (210, 140, 500, 155), text="b", row=0, col=1,
           cell_role="value"),
        mk("p/t/1/0", "cell", 4, (72, 160, 200, 175), text="c", row=1, col=0,
           cell_role="label"),
        mk("p/t/1/1", "cell", 5, (210, 160, 500, 175), text="d", row=1, col=1,
           cell_role="value"),
    ]
    table = mk("p/t", "table", 1, None, n_rows=2, n_cols=2, children=cells)
    root = mk("p", "part", 0, None, title="P", children=[table])
    extents(root)
    violations, _, _ = check_tree("p", root)
    assert violations == []


def test_siblings_that_overlap_vertically_are_a_violation():
    a = mk("p/1/a", "item", 2, (110, 130, 400, 160), label="(a)", text="one")
    b = mk("p/1/b", "item", 3, (110, 150, 400, 180), label="(b)", text="two")
    parent = mk("p/1", "heading", 1, (100, 100, 400, 115), label="1", title="One",
                children=[a, b])
    root = mk("p", "part", 0, None, title="P", children=[parent])
    extents(root)
    hits = failures(root, "sibling_overlap")
    assert len(hits) == 1 and "overlaps sibling" in hits[0].detail


def test_a_sibling_entirely_above_its_predecessor_is_out_of_reading_order():
    a = mk("p/1/a", "item", 2, (110, 200, 400, 220), label="(a)", text="one")
    b = mk("p/1/b", "item", 3, (110, 130, 400, 150), label="(b)", text="two")
    parent = mk("p/1", "heading", 1, (100, 100, 400, 115), label="1", title="One",
                children=[a, b])
    root = mk("p", "part", 0, None, title="P", children=[parent])
    extents(root)
    hits = failures(root, "siblings_ascend")
    assert len(hits) == 1 and "entirely above" in hits[0].detail


# --------------------------------- geometry tolerances come from config, SPEC 2.1

def stacked(prev_box, next_box):
    a = mk("p/1/a", "item", 2, prev_box, label="(a)", text="one")
    b = mk("p/1/b", "item", 3, next_box, label="(b)", text="two")
    parent = mk("p/1", "heading", 1, (40, 40, 600, 55), label="1", title="One",
                children=[a, b])
    root = mk("p", "part", 0, None, title="P", children=[parent])
    extents(root)
    return root


def test_the_tolerances_are_read_from_config_not_redefined_here():
    """SPEC 2.1: one number, two readers. Stage 2 and stage 8 check the same
    invariants over the same trees, so the slack has to come from one place."""
    assert invariants.INDENT_TOLERANCE == config.PARSE_GEOMETRY["indent_tolerance"]
    assert invariants.VERTICAL_TOLERANCE == config.PARSE_GEOMETRY["vertical_tolerance"]
    assert invariants.SIBLING_OVERLAP_SHARE == config.PARSE_GEOMETRY["sibling_overlap_share"]


def test_horizontal_and_vertical_jitter_are_different_numbers():
    """Reusing indent_tolerance for the vertical comparisons was a guess and it
    was wrong: the parser has a separate, smaller vertical_tolerance."""
    assert invariants.VERTICAL_TOLERANCE != invariants.INDENT_TOLERANCE


def test_own_box_above_first_child_uses_vertical_not_indent_tolerance():
    """A 1.5pt dip is inside indent_tolerance (2.0) and outside
    vertical_tolerance (1.0), so this pair distinguishes the two readings."""
    child = mk("p/1/a", "item", 2, (110, 98.5, 400, 145), label="(a)", text="body")
    parent = mk("p/1", "heading", 1, (100, 100, 400, 115), label="1", title="One",
                children=[child])
    root = mk("p", "part", 0, None, title="P", children=[parent])
    extents(root)
    assert [v.path for v in failures(root, "own_box_above_first_child")] == ["p/1"]


def test_the_ascent_half_of_siblings_ascend_uses_vertical_tolerance():
    """Next sits entirely above prev by a hair: inside vertical_tolerance it is
    tolerated, beyond it it is a reading-order fault."""
    within = stacked((100, 130, 400, 150), (100, 129.5, 400, 149.5))
    assert failures(within, "siblings_ascend") == []
    beyond = stacked((100, 130, 400, 150), (100, 100, 400, 128))
    assert [v.check for v in failures(beyond, "siblings_ascend")] == ["siblings_ascend"]


@pytest.mark.parametrize("name,prev_box,next_box", [
    # Real boxes from the preserved parser run, which stage 2 reports clean.
    ("award-form/8 vs 7, 2.38pt on a 58.58pt box",
     (57.8, 631.33, 409.94, 689.91), (57.8, 687.53, 456.74, 751.9)),
    ("award-form/row-2 vs 10, 3.38pt on a 17.18pt box, share 0.197",
     (57.8, 432.33, 121.86, 449.51), (75.8, 446.13, 545.78, 492.77)),
    ("award-form/12 vs 11, 2.38pt on a 17.18pt box",
     (57.8, 470.53, 443.79, 501.51), (57.8, 499.13, 497.14, 516.31)),
])
def test_line_box_overlap_within_the_configured_share_is_not_a_violation(
        name, prev_box, next_box):
    """A line box spans ascent plus descent, so consecutive lines overlap by
    0.8 to 2.9pt purely by construction. Flagging that produced 117 sibling
    violations against stage 2's 0 over identical trees."""
    assert failures(stacked(prev_box, next_box), "sibling_overlap") == [], name


def test_overlap_beyond_the_configured_share_is_still_a_violation():
    """The tolerance must not swallow a real collision: half a 20pt box."""
    root = stacked((100, 100, 400, 120), (100, 110, 400, 130))
    hits = failures(root, "sibling_overlap")
    assert len(hits) == 1
    assert "10.00pt" in hits[0].detail
    assert "sibling_overlap_share=0.2" in hits[0].detail


def test_the_share_is_of_the_smaller_box():
    tol = invariants.sibling_overlap_tolerance((0, 0, 10, 100), (0, 0, 10, 10))
    assert tol == pytest.approx(config.PARSE_GEOMETRY["sibling_overlap_share"] * 10)


def test_the_overlap_tolerance_is_floored_at_vertical_tolerance():
    """Below a 5pt box the proportional allowance falls under plain baseline
    jitter, so the floor takes over. 5pt is vertical_tolerance / share."""
    share = config.PARSE_GEOMETRY["sibling_overlap_share"]
    floor = config.PARSE_GEOMETRY["vertical_tolerance"]
    assert invariants.sibling_overlap_tolerance((0, 0, 10, 2), (0, 0, 10, 2)) == floor
    assert share * 2 < floor, "the floor is doing real work on a 2pt box"
    # Just above the crossover the share wins again.
    tall = invariants.sibling_overlap_tolerance((0, 0, 10, 20), (0, 0, 10, 20))
    assert tall == pytest.approx(share * 20)
    assert tall > floor


def test_the_floor_does_not_change_the_preserved_parser_pairs():
    """Every real pair is on a box far above the 5pt crossover, so adding the
    floor left the three regression expectations above untouched."""
    for prev_box, next_box in [((57.8, 631.33, 409.94, 689.91), (57.8, 687.53, 456.74, 751.9)),
                               ((57.8, 432.33, 121.86, 449.51), (75.8, 446.13, 545.78, 492.77)),
                               ((57.8, 470.53, 443.79, 501.51), (57.8, 499.13, 497.14, 516.31))]:
        heights = (prev_box[3] - prev_box[1], next_box[3] - next_box[1])
        share_only = config.PARSE_GEOMETRY["sibling_overlap_share"] * min(heights)
        assert invariants.sibling_overlap_tolerance(prev_box, next_box) == share_only


def test_an_indent_within_the_configured_tolerance_is_not_a_violation():
    """3.1 at x=27.0 with its child 3.1.1 at 26.4 is glyph jitter, not a fault."""
    assert failures(clean_pair(child_box=(99.0, 130, 400, 145),
                               parent_box=(100.0, 100, 400, 115)),
                    "child_left_edge") == []


def test_an_indent_beyond_the_configured_tolerance_is_still_a_violation():
    root = clean_pair(child_box=(96.0, 130, 400, 145), parent_box=(100.0, 100, 400, 115))
    assert [v.path for v in failures(root, "child_left_edge")] == ["p/1/a"]


def test_reading_order_and_overlap_are_separate_check_ids():
    """SPEC 2.1 pins both; they are different faults and stage 2 reports them
    apart, so a report that merges them cannot be diffed against stage 2's."""
    assert "siblings_ascend" in CHECKS and "sibling_overlap" in CHECKS
    above = stacked((100, 200, 400, 220), (100, 130, 400, 150))
    assert [v.check for v in failures(above, "siblings_ascend")] == ["siblings_ascend"]
    assert failures(above, "sibling_overlap") == []


def test_extent_must_nest_inside_the_parents():
    root = clean_pair()
    root.children[0].children[0].bboxes_extent = [BBox(page=1, bbox=(110, 130, 900, 145))]
    hits = failures(root, "extent_nests")
    assert len(hits) == 1 and "escapes parent extent" in hits[0].detail


def test_numbering_gap_between_siblings():
    a = mk("p/1/1.1", "clause", 2, (110, 130, 400, 145), label="1.1", text="one")
    b = mk("p/1/1.4", "clause", 3, (110, 150, 400, 165), label="1.4", text="two")
    parent = mk("p/1", "heading", 1, (100, 100, 400, 115), label="1", title="One",
                children=[a, b])
    root = mk("p", "part", 0, None, title="P", children=[parent])
    extents(root)
    hits = failures(root, "numbering_gap")
    assert len(hits) == 1 and hits[0].detail == "1.1 is followed by 1.4"


def test_content_hash_must_match_the_text():
    root = clean_pair()
    root.children[0].children[0].content_hash = "0" * 40
    assert [v.path for v in failures(root, "content_hash")] == ["p/1/a"]


def test_order_must_ascend_in_preorder():
    root = clean_pair()
    root.children[0].children[0].order = 0                # duplicate of the root's
    hits = failures(root, "order_preorder")
    assert len(hits) == 1 and "not unique" in hits[0].detail


def test_page_range_of_a_child_must_sit_inside_its_parents():
    root = clean_pair()
    child = root.children[0].children[0]
    child.page_start = child.page_end = 5
    assert [v.check for v in failures(root, "page_range")] == ["page_range"]


@pytest.mark.parametrize("labels,mode,values", [
    (["1.1", "1.2"], "dotted", [1, 2]),
    (["(a)", "(b)"], "letter", [1, 2]),
    (["(i)", "(ii)", "(iv)"], "roman", [1, 2, 4]),
    (["(y)", "(z)"], "letter", [25, 26]),
])
def test_numbering_modes(labels, mode, values):
    assert numbering_mode(labels) == mode
    assert [label_sequence_value(x, mode) for x in labels] == values


def test_roman_one_beats_letter_i_when_the_group_opens_with_it():
    """(i) is both roman one and the ninth letter; the group's opening label decides."""
    assert numbering_mode(["(i)", "(ii)"]) == "roman"
    assert numbering_mode(["(a)", "(b)", "(i)"]) == "letter"
    assert label_sequence_value("(i)", "letter") == 9


def test_anomaly_key_convention():
    assert anomaly_key("numbering_gap_after_9.2: 9.4 follows") == "numbering_gap_after_9.2"
    node = Node(id="x", lineage_key="y", path="p", kind="part", page_start=1,
                page_end=1, order=0, anomalies=["numbering_gap_after_9.2: prose"])
    assert AnomalyLedger().claim("numbering_gap", (node,))
    assert AnomalyLedger().claim("child_left_edge", (node,)) is None


# ------------------------------------------------- anomaly amnesty, blocker 1

def gapped_group(anomalies_92=(), parent_anomalies=(), labels=("9.1", "9.2", "9.5", "9.7")):
    """A sibling group whose numbering jumps, for the amnesty probes."""
    kids = []
    for i, label in enumerate(labels):
        y = 130 + i * 40
        kids.append(mk(f"p/9/{label}", "clause", 2 + i, (110, y, 400, y + 20),
                       label=label, text=f"clause {label}",
                       anomalies=list(anomalies_92) if label == "9.2" else []))
    parent = mk("p/9", "heading", 1, (100, 100, 400, 115), label="9", title="Nine",
                children=kids, anomalies=list(parent_anomalies))
    root = mk("p", "part", 0, None, title="P", children=[parent])
    extents(root)
    return root


def gap_violations(root):
    violations, _, _ = check_tree("p", root)
    return [v for v in violations if v.check == "numbering_gap"]


def test_the_reviewers_probe_two_gaps_need_two_anomalies():
    """9.1, 9.2, 9.5, 9.7 with one recorded anomaly about a *different* gap.

    Before the fix, "numbering_gap_after_9.2: 9.4 follows" explained the 9.2-to-9.5
    jump it contradicts, and the parent's excerpt anomaly explained 9.5-to-9.7.
    Both gaps came out explained and the gate passed on a tree with two
    unrecorded numbering gaps.
    """
    root = gapped_group(
        anomalies_92=["numbering_gap_after_9.2: 9.4 follows in source order"],
        parent_anomalies=["numbering_gap: fixture excerpt jumps from 3 to 9, "
                          "clauses 4 to 8 deliberately not included"])
    gaps = gap_violations(root)
    assert [v.detail for v in gaps] == ["9.2 is followed by 9.5", "9.5 is followed by 9.7"]
    assert len([v for v in gaps if not v.explained_by]) == 2, \
        [(v.detail, v.explained_by) for v in gaps]


def test_a_parents_anomaly_never_explains_what_happened_between_its_children():
    root = gapped_group(parent_anomalies=["numbering_gap: something about the group"],
                        labels=("9.1", "9.2", "9.5"))
    assert [v.explained_by for v in gap_violations(root)] == [None]


def test_an_anomaly_naming_a_different_follower_does_not_explain_the_gap():
    root = gapped_group(anomalies_92=["numbering_gap_after_9.2: 9.4 follows in source order"],
                        labels=("9.1", "9.2", "9.5"))
    assert [v.explained_by for v in gap_violations(root)] == [None]


def test_an_anomaly_naming_the_observed_follower_does_explain_the_gap():
    """The consistency rule must not swing the other way and reject good ones."""
    root = gapped_group(anomalies_92=["numbering_gap_after_9.2: 9.5 follows, 9.3 and 9.4 "
                                      "are not in the source"],
                        labels=("9.1", "9.2", "9.5"))
    assert [v.explained_by for v in gap_violations(root)] == [
        "numbering_gap_after_9.2: 9.5 follows, 9.3 and 9.4 are not in the source"]


def test_an_anomaly_naming_no_labels_is_a_generic_explanation():
    root = gapped_group(anomalies_92=["numbering_gap: the source skips ahead here"],
                        labels=("9.1", "9.2", "9.5"))
    assert gap_violations(root)[0].explained_by is not None


def test_one_anomaly_explains_one_violation_not_a_whole_group():
    """Two gaps, one generic anomaly on the node between them: one stays bare."""
    root = gapped_group(anomalies_92=["numbering_gap: the source skips ahead here"],
                        labels=("9.1", "9.2", "9.5", "9.7"))
    gaps = gap_violations(root)
    assert len(gaps) == 2
    assert len([v for v in gaps if v.explained_by]) == 1
    assert len([v for v in gaps if not v.explained_by]) == 1


def test_the_shipped_fixture_anomaly_still_explains_the_three_to_nine_gap(workspace):
    """The location rule must not break the real fixture: master's anomaly sits
    on core-terms/9, which is the violating node itself."""
    run = workspace.run()
    invariants = run.section("invariants")
    assert invariants["totals"]["unexplained"] == 0
    assert invariants["violations"][0]["check"] == "numbering_gap"
    assert invariants["violations"][0]["explained_by"].startswith("numbering_gap:")
    assert run.code == 0


def test_every_check_id_is_described():
    for check in CHECKS.values():
        assert check and check[0].islower()
