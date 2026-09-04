"""Naming provisions the way the agreement names them.

"Core Terms, Clause 9.2", not "core-terms/9/9.2". Both UIs need this — the
review queue to label a row, the chat to label a footnote and a node on the
connections map — so it lives in the shared package beside source.py.

Built from the part's own title and the deepest unit the document itself
numbers, using the unit label the document uses (SPEC 2.1), so a reader sees
the reference in the form they would write it themselves.
"""
from __future__ import annotations

from .source import part_of

FAMILY_WORDS = {
    "core-terms": ("the Core Terms", "the clauses that govern the agreement as a whole"),
    "award-form": ("the Framework Award Form", "the form that records who the agreement is with"),
    "framework-schedule": ("a Framework Schedule", "part of the framework agreement itself"),
    "joint-schedule": ("a Joint Schedule", "shared by the framework agreement and the contracts called off under it"),
    "call-off-schedule": ("a Call-Off Schedule", "part of an individual contract called off under the framework"),
}

_CELL_ROLE_WORDS = {"label": "the label", "value": "the entry", "header": "the heading"}
_KIND_UNITS = {"form_row": "row", "table": "table", "item": "paragraph"}


def family_words(path: str) -> tuple[str, str]:
    """(what it is, what that means) for the part a path belongs to."""
    part = part_of(path)
    for prefix, words in FAMILY_WORDS.items():
        if part == prefix or part.startswith(prefix + "-"):
            return words
    return (part.replace("-", " "), "")


def title_case_part(part: str) -> str:
    """`framework-schedule-2` -> `Framework Schedule 2`."""
    words: list[str] = []
    for token in part.split("-"):
        if token.isdigit():
            words.append(token)
        elif token.lower() == "off" and words:
            words[-1] = words[-1] + "-Off"
        else:
            words.append(token.capitalize())
    return " ".join(words)


def _definition_label_cell(c, node):
    """For a definitions-table value cell, the cell holding the quoted term."""
    parts = node.path.rsplit("/", 2)
    if len(parts) != 3 or node.col in (None, 0):
        return None
    return c.node(f"{parts[0]}/{parts[1]}/0")


def human_citation(c, node) -> str:
    if node is None:
        return ""
    part_node = c.node(part_of(node.path))
    part_name = (part_node.title if part_node is not None and part_node.title
                 else title_case_part(part_of(node.path)))
    if node.kind == "part":
        return part_name

    # Cells and intros carry no number of their own, so name them by what they
    # are rather than by the nearest numbered ancestor, which would read as if
    # the whole clause were meant.
    owner = next((n for n in reversed(c.ancestors(node.path)) if n.label), None)
    if node.kind == "cell":
        label_cell = _definition_label_cell(c, node)
        if label_cell is not None and label_cell.text:
            return f"{part_name}, the definition of {label_cell.text.strip()}"
        if node.col == 0 and node.text and node.text.strip().startswith('"'):
            return f"{part_name}, the term {node.text.strip()}"
        if owner is not None:
            role = _CELL_ROLE_WORDS.get(node.cell_role or "", "a cell")
            noun = "row" if owner.kind == "form_row" else (owner.unit_label or "row")
            return f"{part_name}, {noun} {owner.label}, {role}"
        return part_name
    if node.kind == "intro" and owner is not None:
        return f"{part_name}, {(owner.unit_label or 'Clause')} {owner.label}, opening words"

    chain = [n for n in (c.ancestors(node.path) + [node]) if n.label and n.kind != "part"]
    if not chain:
        return part_name

    deepest = chain[-1]
    unit = deepest.unit_label or _KIND_UNITS.get(deepest.kind, deepest.kind.capitalize())
    if len(chain) > 1 and deepest.label.startswith("("):
        parent = chain[-2]
        punit = parent.unit_label or "Clause"
        return f"{part_name}, {punit} {parent.label}, {unit.lower()} {deepest.label}"
    return f"{part_name}, {unit} {deepest.label}"


def name_for_path(c, path: str) -> str:
    """Human name for a path, whether or not the node is loaded."""
    node = c.node(path)
    return human_citation(c, node) if node is not None else title_case_part(path)
