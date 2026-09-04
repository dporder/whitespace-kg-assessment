"""Synthetic trees for the stage 4 tests.

Built here rather than copied from `fixtures/`, for two reasons. The fixtures are
the orchestrator's and a worker must not edit them, and these tests need shapes
the fixtures deliberately do not contain: a part-local definitions block that
shadows the document-level one, a definition cell that prints the definitional
verb, an alias that could bind to two terms, a heading that names a term.

The text is invented for the test. Where it mimics the real document it mimics
the *shape* (a closing quote with no opening one, a first letter absent from a
wrapped term), never the words, per the SPEC ground rule that nothing from the
PDF enters the repo outside `output/`.
"""
from __future__ import annotations

import pytest

from pipeline.schemas import BBox, Node, content_hash, lineage_key, node_id

DOC = "rm6116-test"
VERSION = "vt"


def mk(path: str, kind: str, *, order: int, page: int = 1, **kw) -> Node:
    text = kw.get("text")
    kw.setdefault("bboxes_own", [BBox(page=page, bbox=(72.0, 100.0, 480.0, 115.0))])
    return Node(id=node_id(DOC, VERSION, path), lineage_key=lineage_key(DOC, path),
                content_hash=content_hash(text) if text else None,
                path=path, kind=kind, page_start=page, page_end=page, order=order, **kw)


def cell(path: str, *, order: int, row: int, col: int, role: str, text: str) -> Node:
    return mk(path, "cell", order=order, row=row, col=col, cell_role=role,
              role_confidence=0.99, text=text)


def definitions_table(part_path: str, lead_in: str, rows: list[tuple[str, str]],
                      *, start_order: int = 1) -> tuple[Node, Node, list[Node]]:
    """(lead-in node, table node, value cells) for a two-column definitions block."""
    order = start_order
    intro = mk(f"{part_path}/1/intro", "intro", order=order, text=lead_in, citable=False)
    order += 1
    table_path = f"{part_path}/1/table"
    cells: list[Node] = []
    values: list[Node] = []
    for i, (term, definition) in enumerate(rows):
        order += 1
        label = cell(f"{table_path}/{i}/0", order=order, row=i, col=0,
                     role="label", text=term)
        order += 1
        value = cell(f"{table_path}/{i}/1", order=order, row=i, col=1,
                     role="value", text=definition)
        cells.extend([label, value])
        values.append(value)
    table = mk(table_path, "table", order=start_order + 1, n_rows=len(rows), n_cols=2,
               children=cells)
    return intro, table, values


@pytest.fixture
def document_definitions_part() -> Node:
    """A document-level definitions schedule, with the pack's real ink defects:
    a closing quote and no opening one, and a term whose first letter is absent."""
    intro, table, _values = definitions_table(
        "defs-schedule",
        "In each Contract, unless the context otherwise requires, the following "
        "words shall have the following meanings:",
        [('Widget"', "means an item supplied under a Contract;"),
         ('Widget Register"', "the register of each Widget kept by the Holding Body;"),
         ('nsurances"', "the policies the Holder must maintain;"),
         ('"Holding Body" ("HB")', "the body that holds the register;"),
         ('Delegated Item"', "has the meaning given in Schedule 6 (Delegation);")])
    head = mk("defs-schedule/1", "heading", order=1, label="1", title="Definitions",
              children=[intro, table])
    return mk("defs-schedule", "part", order=0, title="Joint Schedule 1 (Definitions)",
              part_family="joint-schedule", children=[head])


@pytest.fixture
def clauses_part() -> Node:
    """A clause part that uses the vocabulary and defines two terms of its own."""
    local_intro = mk("clauses/1/intro", "intro", order=2, citable=False,
                     text="In this Schedule, the following words shall have the "
                          "following meanings and they shall supplement the "
                          "Definitions Schedule:")
    local_label = cell("clauses/1/table/0/0", order=4, row=0, col=0, role="label",
                       text='"Widget" ')
    local_value = cell("clauses/1/table/0/1", order=5, row=0, col=1, role="value",
                       text="means a device specific to this Schedule;")
    local_label2 = cell("clauses/1/table/1/0", order=6, row=1, col=0, role="label",
                        text='"Handover Body" ("HB")')
    local_value2 = cell("clauses/1/table/1/1", order=7, row=1, col=1, role="value",
                        text="the body that receives the Widget on handover;")
    local_table = mk("clauses/1/table", "table", order=3, n_rows=2, n_cols=2,
                     children=[local_label, local_value, local_label2, local_value2])
    head1 = mk("clauses/1", "heading", order=1, label="1", title="Definitions",
               children=[local_intro, local_table])

    body = mk("clauses/2/2.1", "clause", order=9, label="2.1",
              text="Widget Register entries must name the Widget and the Holding "
                   "Body. The Holder shall notify HB of each Widget.")
    heading2 = mk("clauses/2", "heading", order=8, label="2",
                  title="Widget Register duties", children=[body])
    return mk("clauses", "part", order=0, title="Call-Off Schedule 9 (Widgets)",
              part_family="call-off-schedule", children=[head1, heading2])


@pytest.fixture
def prose_part() -> Node:
    """Prose definitions and the two parenthetical conventions side by side."""
    a = mk("prose/1/1.1", "clause", order=2, label="1.1",
           text='In this Schedule, "Reference Body" means the body named in the '
                'Order Form.')
    b = mk("prose/1/1.2", "clause", order=3, label="1.2",
           text="The Central Widget Office (CWO) shall keep the register, and any "
                "instrument of the kind described in the annex (\"Named Papers\") "
                "shall be treated as part of it.")
    head = mk("prose/1", "heading", order=1, label="1", title="Interpretation",
              children=[a, b])
    return mk("prose", "part", order=0, title="Framework Schedule 3 (Prose)",
              part_family="framework-schedule", children=[head])
