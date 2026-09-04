"""Builds the hand-made fixtures. Owned by the orchestrator.

Run: .venv/bin/python fixtures/make_fixtures.py   (from the repo root)

The JSON files this writes are the contract examples every worker builds
against before real pipeline output exists. The text is synthetic mimicry,
not copied from the PDF (SPEC ground rule: nothing from the document enters
the repo outside output/), but the structures are exactly the ones the real
document exhibits: the bare grouping sub-heading (3.1), the intro-plus-items
sandwich (9.1), ragged depth, a ref-bearing intro, a list phrase split into
grouped refs, an unresolved ref to a part that has not arrived, an ambiguous
bare schedule ref, an external legislation ref, the two-column definitions
table with a delegating definition, form rows with placeholder values and the
stray-character label typo, aliases, and a sentence-initial ambiguous term use.

Geometry is fabricated but satisfies the stage 2 invariants: a child's left
edge at or right of its parent's, own box above first child, siblings
ascending without vertical overlap, extents nesting.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.schemas import (  # noqa: E402
    BBox, Candidate, Concept, ConceptRelation, DefinitionSite, Node, RefsFile,
    TermUse, content_hash, lineage_key, node_id,
)

DOC = "rm6116-fixture"
VERSION = "v1"
FIXTURES = Path(__file__).parent


def mk(path: str, kind: str, page: int, order: int, box: tuple | None = None,
       page_end: int | None = None, **kw) -> Node:
    """Node factory: identity, hashes and own-box bookkeeping in one place."""
    if box is not None:
        kw.setdefault("bboxes_own", [BBox(page=page, bbox=box)])
    text = kw.get("text")
    return Node(
        id=node_id(DOC, VERSION, path),
        lineage_key=lineage_key(DOC, path),
        content_hash=content_hash(text) if text else None,
        path=path, kind=kind, page_start=page, page_end=page_end or page,
        order=order, **kw,
    )


def ref(parent: Node, pointing: str, ref_kind: str, scope_rule: str,
        status: str, order: int, resolver: str, box: tuple,
        target_path: str | None = None, candidates: list[Candidate] = [],
        confidence: float | None = None, group_id: str | None = None,
        occurrence: int = 1) -> Node:
    """A ref child anchored to `pointing` inside the parent's text."""
    start = -1
    for _ in range(occurrence):
        start = parent.text.index(pointing, start + 1)
    span = (start, start + len(pointing))
    path = f"{parent.path}/ref@{span[0]}-{span[1]}"
    return Node(
        id=node_id(DOC, VERSION, path), lineage_key=lineage_key(DOC, path),
        path=path, kind="ref", text=pointing, char_span=span, citable=False,
        page_start=parent.page_start, page_end=parent.page_start, order=order,
        bboxes_own=[BBox(page=parent.page_start, bbox=box)],
        ref_kind=ref_kind, scope_rule=scope_rule, status=status,
        resolver=resolver, target_path=target_path, candidates=candidates,
        confidence=confidence, group_id=group_id, batch_id=parent.batch_id,
    )


def compute_extents(node: Node) -> dict[int, list[float]]:
    """extent = union per page of own box and every descendant's extent."""
    boxes: dict[int, list[float]] = {}

    def merge(page: int, b) -> None:
        if page in boxes:
            x0, y0, x1, y1 = boxes[page]
            boxes[page] = [min(x0, b[0]), min(y0, b[1]), max(x1, b[2]), max(y1, b[3])]
        else:
            boxes[page] = list(b)

    for bb in node.bboxes_own:
        merge(bb.page, bb.bbox)
    for child in node.children:
        for page, b in compute_extents(child).items():
            merge(page, b)
    node.bboxes_extent = [BBox(page=p, bbox=tuple(b)) for p, b in sorted(boxes.items())]
    return boxes


# ---------------------------------------------------------------- core terms
B1 = dict(batch_id="B1", unit_label="Clause", unit_label_source="document")
ITEM = dict(batch_id="B1", unit_label="Paragraph", unit_label_source="profile")

intro_311 = mk("core-terms/3/3.1/3.1.1/intro", "intro", 1, 4, (100, 150, 420, 165),
               text="The Provider must supply Outputs:", citable=False,
               batch_id="B1", printed_page="1")
item_311a = mk("core-terms/3/3.1/3.1.1/a", "item", 1, 5, (114, 170, 480, 185),
               label="(a)", text="that meet the Requirement and comply with Law; and",
               printed_page="1", **ITEM)
item_311b = mk("core-terms/3/3.1/3.1.1/b", "item", 1, 6, (114, 190, 480, 205),
               label="(b)", text="Good Working Practice applies to everything supplied.",
               printed_page="1", **ITEM)
sub_311 = mk("core-terms/3/3.1/3.1.1", "subclause", 1, 3, (100, 150, 112, 165),
             label="3.1.1", children=[intro_311, item_311a, item_311b],
             printed_page="1", **B1)
sub_312 = mk("core-terms/3/3.1/3.1.2", "subclause", 1, 7, (100, 215, 480, 245),
             label="3.1.2", printed_page="1",
             text="The Provider must supply Outputs with a warranty of at least "
                  "90 days from Handover against all obvious defects.", **B1)
# The bare grouping quirk: numbering says clause level, function says heading.
head_31 = mk("core-terms/3/3.1", "heading", 1, 2, (86, 120, 250, 136),
             label="3.1", title="All outputs", children=[sub_311, sub_312],
             printed_page="1", **B1)
head_3 = mk("core-terms/3", "heading", 1, 1, (72, 90, 340, 106),
            label="3", title="What has to be provided", children=[head_31],
            printed_page="1", **B1)

intro_91 = mk("core-terms/9/9.1/intro", "intro", 2, 10, (100, 120, 480, 150),
              text="Subject to Clause 3.1.2 and Framework Schedule 4 (Framework "
                   "Management), each Party keeps ownership of its own Existing "
                   "IPRs to enable it to both:", citable=False,
              batch_id="B1", printed_page="2")
item_91a = mk("core-terms/9/9.1/a", "item", 2, 11, (114, 155, 460, 170),
              label="(a)", text="receive and use the Outputs; and",
              printed_page="2", **ITEM)
item_91b = mk("core-terms/9/9.1/b", "item", 2, 12, (114, 175, 470, 190),
              label="(b)", text="make use of outputs supplied to CBO by a Replacement Provider.",
              printed_page="2", **ITEM)
clause_91 = mk("core-terms/9/9.1", "clause", 2, 9, (86, 120, 98, 135),
               label="9.1", children=[intro_91, item_91a, item_91b],
               printed_page="2", **B1)
clause_92 = mk("core-terms/9/9.2", "clause", 2, 13, (86, 205, 490, 250),
               label="9.2", printed_page="2",
               text="Any New IPR created under a Contract is owned by the Central "
                    "Buying Office subject to Clauses 3.1.1 and 3.1.2, Schedule 2 "
                    "and the Bribery Act 2010.",
               anomalies=["numbering_gap_after_9.2: 9.4 follows in source order"], **B1)
head_9 = mk("core-terms/9", "heading", 2, 8, (72, 90, 360, 106),
            label="9", title="Intellectual Property Rights", children=[clause_91, clause_92],
            printed_page="2", **B1)

core_part = mk("core-terms", "part", 1, 0, None, page_end=2,
               title="Core Terms", part_family="core", template_version="v3.0.11",
               children=[head_3, head_9], batch_id="B1",
               unit_label="Clause", unit_label_source="document")

core_refs = RefsFile(part="core-terms", refs=[
    ref(intro_91, "Clause 3.1.2", "clause", "js1_1.3.8", "resolved", 0, "scope",
        (155, 120, 215, 133), target_path="core-terms/3/3.1/3.1.2", confidence=None),
    ref(intro_91, "Framework Schedule 4 (Framework Management)", "schedule",
        "title_paren", "unresolved", 1, "scope", (230, 120, 460, 133),
        candidates=[Candidate(path="framework-schedule-4", score=0.95,
                              reason="title parenthetical names the family; part not ingested")]),
    ref(clause_92, "3.1.1", "clause", "js1_1.3.8", "resolved", 2, "scope",
        (300, 220, 330, 233), target_path="core-terms/3/3.1/3.1.1", group_id="g-9.2-1"),
    ref(clause_92, "3.1.2", "clause", "js1_1.3.8", "resolved", 3, "scope",
        (340, 220, 370, 233), target_path="core-terms/3/3.1/3.1.2", group_id="g-9.2-1"),
    ref(clause_92, "Schedule 2", "schedule", "none", "ambiguous", 4, "llm",
        (380, 220, 435, 233), confidence=0.45,
        candidates=[
            Candidate(path="framework-schedule-2", score=0.5, reason="bare number, no title parenthetical"),
            Candidate(path="call-off-schedule-2", score=0.5, reason="bare number, no title parenthetical"),
        ]),
    ref(clause_92, "Bribery Act 2010", "legislation", "none", "external", 5, "grammar",
        (200, 235, 290, 248), target_path="legislation/bribery-act-2010"),
])

# ---------------------------------------------------------------- award form
form_name_label = mk("award-form/2/label", "cell", 1, 2, (72, 130, 180, 145),
                     text="Provider", row=2, col=0, cell_role="label",
                     role_confidence=0.99, batch_id="B3", printed_page="1")
form_name_value = mk("award-form/2/value", "cell", 1, 3, (200, 130, 470, 145),
                     text="[Insert name (registered name if registered)]",
                     row=2, col=1, cell_role="value", role_confidence=0.99,
                     batch_id="B3", printed_page="1")
form_row_2 = mk("award-form/2", "form_row", 1, 1, None, label="2",
                children=[form_name_label, form_name_value], batch_id="B3",
                printed_page="1")

form_fc_label = mk("award-form/3/label", "cell", 1, 5, (72, 160, 180, 190),
                   text="rFramework Contract", row=3, col=0, cell_role="label",
                   role_confidence=0.98, batch_id="B3", printed_page="1",
                   anomalies=["stray_character_in_label: 'rFramework' for 'Framework', recorded verbatim"])
form_fc_value = mk("award-form/3/value", "cell", 1, 6, (200, 160, 490, 205),
                   text="This framework contract between the Central Buying Office "
                        "and the Provider allows the Provider to be considered for "
                        "Call-Off Contracts to supply the Outputs.",
                   row=3, col=1, cell_role="value", role_confidence=0.99,
                   batch_id="B3", printed_page="1")
form_row_3 = mk("award-form/3", "form_row", 1, 4, None, label="3",
                children=[form_fc_label, form_fc_value], batch_id="B3",
                printed_page="1")

award_part = mk("award-form", "part", 1, 0, None, title="Framework Award Form",
                part_family="award-form", template_version="v3.10",
                children=[form_row_2, form_row_3], batch_id="B3")

# ---------------------------------------------------- joint schedule 1 table
JS1 = dict(batch_id="B2", printed_page="1")
c00 = mk("joint-schedule-1/2/table/0/0", "cell", 1, 3, (72, 140, 200, 155),
         text='"Provider"', row=0, col=0, cell_role="label", role_confidence=0.99, **JS1)
c01 = mk("joint-schedule-1/2/table/0/1", "cell", 1, 4, (210, 140, 500, 155),
         text="the person named as supplier in the Order Form;",
         row=0, col=1, cell_role="value", role_confidence=0.99, **JS1)
c10 = mk("joint-schedule-1/2/table/1/0", "cell", 1, 5, (72, 160, 200, 175),
         text='"Materials"', row=1, col=0, cell_role="label", role_confidence=0.99, **JS1)
c11 = mk("joint-schedule-1/2/table/1/1", "cell", 1, 6, (210, 160, 500, 190),
         text="all Outputs supplied by the Provider, as described in Schedule 6 (Materials);",
         row=1, col=1, cell_role="value", role_confidence=0.99, **JS1)
c20 = mk("joint-schedule-1/2/table/2/0", "cell", 1, 7, (72, 195, 200, 225),
         text='"Central Buying Office" ("CBO")', row=2, col=0, cell_role="label",
         role_confidence=0.99, **JS1)
c21 = mk("joint-schedule-1/2/table/2/1", "cell", 1, 8, (210, 195, 500, 210),
         text="the central purchasing authority;", row=2, col=1, cell_role="value",
         role_confidence=0.99, **JS1)
c30 = mk("joint-schedule-1/2/table/3/0", "cell", 1, 9, (72, 230, 200, 260),
         text='"Good Working Practice"', row=3, col=0, cell_role="label",
         role_confidence=0.99, **JS1)
c31 = mk("joint-schedule-1/2/table/3/1", "cell", 1, 10, (210, 230, 500, 245),
         text="standards which a skilled person would reasonably be expected to meet;",
         row=3, col=1, cell_role="value", role_confidence=0.99, **JS1)
js1_table = mk("joint-schedule-1/2/table", "table", 1, 2, None, n_rows=4, n_cols=2,
               children=[c00, c01, c10, c11, c20, c21, c30, c31], **JS1)
js1_head = mk("joint-schedule-1/2", "heading", 1, 1, (72, 110, 260, 126),
              label="2", title="Defined Terms", children=[js1_table],
              batch_id="B2", printed_page="1",
              unit_label="Paragraph", unit_label_source="document")
js1_part = mk("joint-schedule-1", "part", 1, 0, None,
              title="Joint Schedule 1 (Definitions)", part_family="joint-schedule",
              template_version="v3.10", children=[js1_head], batch_id="B2",
              unit_label="Paragraph", unit_label_source="document")

js1_refs = RefsFile(part="joint-schedule-1", refs=[
    ref(c11, "Schedule 6 (Materials)", "schedule", "title_paren", "unresolved",
        0, "scope", (400, 160, 495, 175),
        candidates=[Candidate(path="call-off-schedule-6", score=0.9,
                              reason="title parenthetical matches; part not ingested")]),
])

# ------------------------------------------------------------------- vocab
definition_sites = [
    DefinitionSite(term="Provider", definition_node_id=c01.id, source="declared",
                   scope="document"),
    DefinitionSite(term="Materials", definition_node_id=c11.id, source="declared",
                   scope="document", pointer="Schedule 6"),
    DefinitionSite(term="Central Buying Office", definition_node_id=c21.id,
                   source="both", scope="document", aliases=["CBO"]),
    DefinitionSite(term="Good Working Practice", definition_node_id=c31.id,
                   source="declared", scope="document"),
]

def use(term: str, node: Node, matched: str | None = None, **kw) -> TermUse:
    """Span computed from the node's own text so it cannot drift.
    `matched` is the surface form when it differs from the term (aliases)."""
    surface = matched or term
    start = node.text.index(surface)
    kw.setdefault("status", "confident")
    kw.setdefault("method", "exact_longest")
    kw.setdefault("definition_used", "document")
    return TermUse(term=term, node_id=node.id,
                   char_span=(start, start + len(surface)), **kw)


term_uses = [
    use("Provider", intro_311),
    use("Outputs", intro_311),
    use("Good Working Practice", item_311b,
        status="ambiguous", ambiguity_kind="sentence_initial"),
    use("Provider", sub_312),
    use("Central Buying Office", clause_92),
    # Alias use: the record carries the canonical term, the span covers "CBO".
    use("Central Buying Office", item_91b, matched="CBO"),
    # Uses inside a definition text: the DEFINED_USING raw material.
    use("Outputs", c11),
    use("Provider", c11),
]

# ----------------------------------------------------------------- concepts
concepts = [
    Concept(id="concept-ip-ownership", label="intellectual property ownership",
            scope_path="core-terms/9",
            member_node_ids=[clause_91.id, clause_92.id], confidence=0.82,
            relations=[ConceptRelation(src="concept-ip-ownership",
                                       label="depends_on",
                                       dst="concept-supply-obligations")]),
    Concept(id="concept-supply-obligations", label="supply obligations",
            scope_path="core-terms/3",
            member_node_ids=[sub_311.id, sub_312.id], confidence=0.78),
]


def dump(obj, rel: str) -> None:
    path = FIXTURES / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, list):
        payload = [o.model_dump(exclude_none=True, exclude_defaults=True) for o in obj]
    else:
        payload = obj.model_dump(exclude_none=True, exclude_defaults=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {rel}")


def main() -> None:
    for part in (core_part, award_part, js1_part):
        compute_extents(part)
        Node.model_validate(part.model_dump())     # round-trip validation
    for span_check in ((core_refs, core_part), (js1_refs, js1_part)):
        refs_file, part = span_check
        by_path = {}

        def walk(n: Node) -> None:
            by_path[n.path] = n
            for c in n.children:
                walk(c)
        walk(part)
        for r in refs_file.refs:
            parent_path = r.path.rsplit("/ref@", 1)[0]
            parent = by_path[parent_path]
            s, e = r.char_span
            assert parent.text[s:e] == r.text, f"span drift on {r.path}"

    dump(core_part, "tree/core-terms.json")
    dump(award_part, "tree/award-form.json")
    dump(js1_part, "tree/joint-schedule-1.json")
    dump(core_refs, "refs/core-terms.json")
    dump(js1_refs, "refs/joint-schedule-1.json")
    dump(definition_sites, "vocab/definition_sites.json")
    dump(term_uses, "vocab/term_uses.json")
    dump(concepts, "concepts.json")
    print("all fixtures validated and written")


if __name__ == "__main__":
    main()
