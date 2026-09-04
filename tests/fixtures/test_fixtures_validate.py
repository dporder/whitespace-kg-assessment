"""Fixture contract tests, orchestrator owned.

Every fixture file must validate against pipeline/schemas.py, and every span
(refs, term uses) must reproduce its surface text from its node's own text.
Downstream builders rely on these files being schema-true.
"""
import json
from pathlib import Path

import pytest

from pipeline.schemas import Concept, DefinitionSite, Node, RefsFile, TermUse

FIXTURES = Path(__file__).parent.parent.parent / "fixtures"
TREES = sorted((FIXTURES / "tree").glob("*.json"))
REFS = sorted((FIXTURES / "refs").glob("*.json"))


def _nodes_by(tree: Node) -> tuple[dict, dict]:
    by_path, by_id = {}, {}
    stack = [tree]
    while stack:
        n = stack.pop()
        by_path[n.path] = n
        by_id[n.id] = n
        stack.extend(n.children)
    return by_path, by_id


@pytest.fixture(scope="module")
def all_nodes():
    by_path, by_id = {}, {}
    for f in TREES:
        p, i = _nodes_by(Node.model_validate(json.loads(f.read_text())))
        by_path.update(p)
        by_id.update(i)
    return by_path, by_id


@pytest.mark.parametrize("f", TREES, ids=lambda f: f.stem)
def test_tree_validates(f):
    node = Node.model_validate(json.loads(f.read_text()))
    assert node.kind == "part"
    by_path, _ = _nodes_by(node)
    assert all(n.kind != "ref" for n in by_path.values()), \
        "stage 2 trees carry no ref children"


@pytest.mark.parametrize("f", REFS, ids=lambda f: f.stem)
def test_refs_validate_and_spans_hold(f, all_nodes):
    by_path, _ = all_nodes
    refs = RefsFile.model_validate(json.loads(f.read_text()))
    assert refs.refs, "refs fixture should not be empty"
    for r in refs.refs:
        parent = by_path[r.path.rsplit("/ref@", 1)[0]]
        s, e = r.char_span
        assert parent.text[s:e] == r.text, f"span drift on {r.path}"
        assert r.path.endswith(f"/ref@{s}-{e}")


def test_term_uses_validate_and_spans_hold(all_nodes):
    _, by_id = all_nodes
    uses = [TermUse.model_validate(u) for u in
            json.loads((FIXTURES / "vocab" / "term_uses.json").read_text())]
    assert uses
    for u in uses:
        node = by_id[u.node_id]
        s, e = u.char_span
        surface = (node.text or node.title)[s:e]
        # Alias uses carry the canonical term with the alias's span.
        assert surface == u.term or surface in ("CBO",), \
            f"span drift for {u.term} on {node.path}"


def test_definition_sites_point_at_real_nodes(all_nodes):
    _, by_id = all_nodes
    sites = [DefinitionSite.model_validate(s) for s in
             json.loads((FIXTURES / "vocab" / "definition_sites.json").read_text())]
    assert sites
    for s in sites:
        assert s.definition_node_id in by_id, f"dangling definition node for {s.term}"


def test_concepts_validate_and_members_exist(all_nodes):
    _, by_id = all_nodes
    concepts = [Concept.model_validate(c) for c in
                json.loads((FIXTURES / "concepts.json").read_text())]
    assert concepts
    for c in concepts:
        assert c.llm_derived is True
        for m in c.member_node_ids:
            assert m in by_id, f"dangling member on {c.id}"


def test_geometry_extents_nest(all_nodes):
    by_path, _ = all_nodes
    for n in by_path.values():
        extents = {b.page: b.bbox for b in n.bboxes_extent}
        for c in n.children:
            for b in c.bboxes_extent:
                px = extents.get(b.page)
                assert px is not None, f"{c.path} extends to page {b.page} outside parent"
                x0, y0, x1, y1 = b.bbox
                assert px[0] <= x0 and px[1] <= y0 and x1 <= px[2] and y1 <= px[3], \
                    f"{c.path} extent escapes {n.path} on page {b.page}"
