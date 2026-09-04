"""Crop rendering, and the guarantees the Neo4j backend has to keep.

The Cypher tests are static: they hold whether or not a graph exists, which is
the point, since the backend has to be trustworthy before stage 7 lands.
"""
import re

import pytest

from chat import crops
from chat.backends import base, get_backend, neo4j_backend, reset
from chat.backends.fixtures import FixturesBackend


# ------------------------------------------------------------------- crops
def test_a_crop_is_a_real_png_from_the_real_pdf():
    png = crops.render_crop(2, [380.0, 220.0, 435.0, 233.0])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000


def test_the_box_is_drawn_in_the_requested_colour():
    """The trust gradient has to survive into the image."""
    import collections

    import pymupdf

    png = crops.render_crop(1, [114.0, 190.0, 480.0, 205.0], colour="rule")
    pix = pymupdf.Pixmap(pymupdf.io.BytesIO(png)) if hasattr(pymupdf, "io") else None
    if pix is None:                       # older binding: write and reopen
        import tempfile
        from pathlib import Path

        p = Path(tempfile.mkstemp(suffix=".png")[1])
        p.write_bytes(png)
        pix = pymupdf.Pixmap(str(p))

    counts = collections.Counter()
    for y in range(pix.height):
        for x in range(pix.width):
            counts[pix.pixel(x, y)] += 1
    saturated = [c for c, n in counts.items() if max(c) - min(c) > 25 and n > 200]
    assert saturated, "no coloured box was drawn"
    r, g, b = max(saturated, key=lambda c: counts[c])
    assert g > r and b > r, f"expected the teal rule colour, got #{r:02x}{g:02x}{b:02x}"


def test_repeated_crops_are_identical():
    """The box is drawn on a copy, so ink cannot accumulate on the cached page."""
    box = [86.0, 205.0, 490.0, 250.0]
    assert crops.render_crop(2, box) == crops.render_crop(2, box)


def test_a_page_outside_the_document_is_an_index_error():
    with pytest.raises(IndexError):
        crops.render_crop(10_000, [1, 1, 20, 20])


def test_a_degenerate_box_is_refused():
    with pytest.raises(ValueError):
        crops.render_crop(1, [100.0, 100.0, 100.0, 100.0])


def test_zoom_changes_the_pixel_size_not_the_region():
    small = crops.render_crop(2, [86.0, 205.0, 490.0, 250.0], zoom=1.0)
    big = crops.render_crop(2, [86.0, 205.0, 490.0, 250.0], zoom=3.0)
    assert len(big) > len(small)


# --------------------------------------------------------- backend switch
def test_the_default_backend_is_the_fixture_one():
    reset()
    assert get_backend().name == "fixtures"


def test_an_unknown_backend_name_is_refused():
    with pytest.raises(ValueError, match="GRAPH_BACKEND"):
        get_backend(force="mongodb")


def test_auto_falls_back_when_no_graph_exists():
    """Tonight there is no loaded graph, so auto must serve the files."""
    assert get_backend(force="auto").name in ("fixtures", "neo4j")


def test_both_backends_declare_the_same_surface():
    for cls in (FixturesBackend, neo4j_backend.Neo4jBackend):
        for tool in base.TOOL_NAMES:
            assert callable(getattr(cls, tool, None)), f"{cls.__name__} is missing {tool}"


# ------------------------------------------------- read-only, parameterised
CYPHER = {
    name: value
    for name, value in vars(neo4j_backend).items()
    if name.startswith("Q_") and isinstance(value, str)
}

WRITES = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|CALL\s*\{[^}]*\bCREATE)\b",
    re.IGNORECASE,
)


def test_there_are_queries_to_check():
    assert len(CYPHER) >= 10


@pytest.mark.parametrize("name", sorted(CYPHER))
def test_no_query_writes(name):
    assert not WRITES.search(CYPHER[name]), f"{name} is not read-only"


def test_every_query_is_a_plain_string_literal():
    """No string-built Cypher, checked structurally rather than by grepping.

    Each Q_* must be a bare constant in the source: not an f-string, not a
    concatenation, not a .format() call. Caller input can then only ever reach
    the database as a $parameter.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(neo4j_backend))
    seen = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("Q_"):
                seen.add(target.id)
                assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str), (
                    f"{target.id} is built at runtime ({type(node.value).__name__}); "
                    "Cypher must be a literal"
                )
    assert seen == set(CYPHER), "a query escaped the check"


@pytest.mark.parametrize("name", sorted(CYPHER))
def test_no_query_carries_a_format_placeholder(name):
    q = CYPHER[name]
    for bad in ("%s", "%d", "%(", ".format(", "{0", "{}"):
        assert bad not in q, f"{name} contains {bad!r}"


def test_every_query_taking_input_uses_a_parameter():
    for name in ("Q_NODE", "Q_DERIVED_TEXT", "Q_CHILDREN", "Q_REFS_OUT",
                 "Q_REFS_IN", "Q_DEFINE", "Q_TERM_BY_ALIAS", "Q_HISTORY"):
        assert "$" in CYPHER[name], f"{name} takes input without a parameter"


def test_availability_is_a_question_not_a_crash(monkeypatch):
    """An unreachable graph must answer False, never raise."""
    monkeypatch.setattr(
        neo4j_backend.Neo4jBackend, "_read",
        lambda self, q, **kw: (_ for _ in ()).throw(OSError("connection refused")),
    )
    assert neo4j_backend.Neo4jBackend.available() is False


# ------------------------------------------------------- bbox decoding
def test_bbox_decoding_accepts_the_encodings_a_loader_might_choose():
    one = '[{"page": 2, "bbox": [1, 2, 3, 4]}]'
    assert neo4j_backend._boxes({"bboxes_own": one})[0]["page"] == 2

    many = ['{"page": 5, "bbox": [1, 2, 3, 4]}']
    assert neo4j_backend._boxes({"bboxes_own": many})[0]["page"] == 5

    flat = {"bboxes_own": [1.0, 2.0, 3.0, 4.0], "page_start": 7}
    assert neo4j_backend._boxes(flat)[0] == {"page": 7, "bbox": [1.0, 2.0, 3.0, 4.0]}


def test_bbox_decoding_degrades_rather_than_guessing():
    assert neo4j_backend._boxes({}) == []
    assert neo4j_backend._boxes({"bboxes_own": "not json"}) == []
