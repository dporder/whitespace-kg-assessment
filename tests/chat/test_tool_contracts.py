"""The seven tool contracts, pinned.

SPEC 6 fixes the tool list and the UIs depend on the return shapes, so this
file is the place a change to either shows up as a failure. Every assertion
runs against the committed fixtures, so it needs no pipeline and no network.
"""
import pytest

from chat import config as ui_config
from chat.backends import FixturesBackend
from chat.backends.base import TOOL_NAMES, VECTOR_PENDING
from chat.source import Corpus, corpus
from chat.tools import TOOL_SCHEMAS, ToolRunner

SPEC_TOOLS = (
    "find_provision",
    "get_provision",
    "follow_references",
    "define",
    "find_by_concept",
    "history",
    "cite",
)


@pytest.fixture(scope="module")
def backend():
    return FixturesBackend()


@pytest.fixture(scope="module")
def c():
    return corpus()


# --------------------------------------------------------------------- list
def test_tool_list_is_exactly_the_spec_list():
    assert TOOL_NAMES == SPEC_TOOLS
    assert tuple(t["name"] for t in TOOL_SCHEMAS) == SPEC_TOOLS


def test_every_schema_is_well_formed():
    for t in TOOL_SCHEMAS:
        assert t["description"].strip()
        schema = t["input_schema"]
        assert schema["type"] == "object"
        for req in schema["required"]:
            assert req in schema["properties"], f"{t['name']} requires undeclared {req}"


# ------------------------------------------------------------ find_provision
def test_find_provision_shape_and_vector_arm(backend):
    out = backend.find_provision("intellectual property")
    assert set(out) == {"query", "backend", "hits", "vector_arm"}
    assert out["backend"] == "fixtures"
    for h in out["hits"]:
        assert set(h) == {"path", "kind", "label", "title", "unit_label",
                          "page", "score", "matched_on"}
        assert 0.0 <= h["score"] <= 1.0
        assert h["matched_on"] in ("path", "title", "label", "text", "term")

    # The embedding arm is behind a flag and says so rather than pretending.
    assert ui_config.EMBEDDING_SEARCH is False
    assert out["vector_arm"] == {"enabled": False, "status": VECTOR_PENDING}


def test_find_provision_never_returns_ref_nodes(backend):
    paths = {h["path"] for h in backend.find_provision("Schedule", limit=50)["hits"]}
    assert not any("/ref@" in p for p in paths)


def test_find_provision_respects_limit(backend):
    assert len(backend.find_provision("Provider", limit=2)["hits"]) <= 2


# ------------------------------------------------------------- get_provision
def test_get_provision_shape(backend):
    out = backend.get_provision("core-terms/9/9.2")
    assert out["found"] is True
    for key in ("path", "kind", "label", "unit_label", "citable", "part",
                "lineage_key", "text", "own_text", "children", "page", "boxes", "anomalies"):
        assert key in out, key
    assert out["kind"] == "clause"
    assert out["label"] == "9.2"
    assert out["page"] == {"start": 2, "end": 2, "printed": "2"}
    assert out["boxes"] and out["boxes"][0]["page"] == 2


def test_get_provision_missing_path_is_not_an_error(backend):
    assert backend.get_provision("core-terms/does/not/exist") == {
        "path": "core-terms/does/not/exist",
        "found": False,
    }


def test_derived_text_walks_children_in_order(backend, c):
    """SPEC 2.1: subtree text is derived by walking `order`, never stored."""
    out = backend.get_provision("core-terms/9/9.1")
    assert out["own_text"] is None, "a container carries no text of its own"
    lines = out["text"].split("\n")
    assert lines == [
        c.by_path["core-terms/9/9.1/intro"].text,
        c.by_path["core-terms/9/9.1/a"].text,
        c.by_path["core-terms/9/9.1/b"].text,
    ]


def test_derived_text_excludes_refs(backend):
    """A ref annotates a span of its parent; it contributes no text of its own."""
    text = backend.get_provision("core-terms/9")["text"]
    assert text.count("Framework Schedule 4 (Framework Management)") == 1


# ---------------------------------------------------------- follow_references
def test_follow_references_outbound(backend):
    out = backend.follow_references("core-terms/9/9.2", "outbound")
    assert out["direction"] == "outbound"
    assert out["count"] == len(out["references"]) == 4
    for r in out["references"]:
        assert set(r) == {"ref_path", "text", "ref_kind", "status", "target_path",
                          "scope_rule", "resolver", "confidence", "group_id",
                          "candidates", "char_span", "page", "from_path",
                          # the names the agreement uses, so a caller never has
                          # to reach past the tools to label what it renders
                          "from_name", "target_name"}
    statuses = sorted(r["status"] for r in out["references"])
    assert statuses == ["ambiguous", "external", "resolved", "resolved"]


def test_follow_references_covers_the_subtree(backend):
    """Outbound from a container finds refs anchored anywhere beneath it."""
    out = backend.follow_references("core-terms/9/9.1", "outbound")
    assert {r["text"] for r in out["references"]} == {
        "Clause 3.1.2",
        "Framework Schedule 4 (Framework Management)",
    }


def test_follow_references_inbound(backend):
    out = backend.follow_references("core-terms/3/3.1/3.1.2", "inbound")
    assert out["count"] == 2
    assert all(r["target_path"] == "core-terms/3/3.1/3.1.2" for r in out["references"])
    assert {r["from_path"] for r in out["references"]} == {
        "core-terms/9/9.1/intro",
        "core-terms/9/9.2",
    }


def test_ambiguous_ref_keeps_its_candidates_and_mints_no_target(backend):
    ref = next(
        r for r in backend.follow_references("core-terms/9/9.2", "outbound")["references"]
        if r["status"] == "ambiguous"
    )
    assert ref["target_path"] is None, "an ambiguous ref must never carry a target"
    assert len(ref["candidates"]) == 2
    assert all(set(cd) == {"path", "score", "reason", "name"} for cd in ref["candidates"])


def test_follow_references_rejects_a_bad_direction(backend):
    with pytest.raises(ValueError):
        backend.follow_references("core-terms/9/9.2", "sideways")


# ---------------------------------------------------------------- define
def test_define_shape(backend):
    out = backend.define("Central Buying Office")
    assert out["found"] is True
    assert out["matched_via"] == "term"
    assert out["aliases"] == ["CBO"]
    site = out["sites"][0]
    assert set(site) == {"term", "scope", "source", "aliases", "pointer",
                         "definition_path", "definition_name", "definition_text", "page"}
    assert site["definition_name"].startswith("Joint Schedule 1")
    assert site["scope"] == "document"
    assert site["definition_path"] == "joint-schedule-1/2/table/2/1"


def test_define_resolves_an_alias(backend):
    out = backend.define("CBO")
    assert out["found"] is True
    assert out["matched_via"] == "alias"
    assert out["term"] == "Central Buying Office"


def test_define_reports_a_delegating_definition(backend):
    assert backend.define("Materials")["sites"][0]["pointer"] == "Schedule 6"


def test_define_unknown_term(backend):
    out = backend.define("Nonexistent Term")
    assert out["found"] is False and out["sites"] == [] and out["governs"] == {}


def test_part_local_definition_shadows_the_document_one():
    """SPEC 2.3, resolution order is part-local first, then document."""
    from pipeline.schemas import DefinitionSite

    c = Corpus.load()
    local = DefinitionSite(
        term="Provider",
        definition_node_id=c.by_path["core-terms/9/9.2"].id,
        source="discovered",
        scope="part:core-terms",
    )
    c.definition_sites.append(local)
    c.sites_by_term.setdefault("Provider", []).append(local)

    b = FixturesBackend(c)
    governs = b.define("Provider")["governs"]
    assert governs["core-terms"]["scope"] == "part:core-terms"
    assert governs["award-form"]["scope"] == "document"
    assert c.governing_site("Provider", "core-terms") is local
    assert c.governing_site("Provider", "award-form").scope == "document"


# --------------------------------------------------------------- concepts
def test_find_by_concept_is_never_citable(backend):
    out = backend.find_by_concept("intellectual property")
    assert out["found"] is True
    assert out["citable"] is False
    con = out["concepts"][0]
    assert con["llm_derived"] is True
    assert con["members"] and all(m["path"] and m["page"] for m in con["members"])


# ---------------------------------------------------------------- history
def test_history_is_wired_and_honest(backend, c):
    key = c.by_path["core-terms/9/9.2"].lineage_key
    out = backend.history(key)
    assert set(out) == {"lineage_key", "count", "versions", "note"}
    assert out["count"] == 1
    assert out["versions"][0]["path"] == "core-terms/9/9.2"
    assert backend.history("no-such-key")["count"] == 0


# ------------------------------------------------------------------- cite
def test_cite_returns_png_bytes_for_the_stored_box(backend):
    out = backend.cite("core-terms/9/9.2")
    assert out["found"] is True
    assert out["page"] == 2
    assert out["bbox"] == [86.0, 205.0, 490.0, 250.0]
    assert out["media_type"] == "image/png"
    assert out["png"][:8] == b"\x89PNG\r\n\x1a\n"
    assert len(out["png"]) > 1000


def test_cite_unknown_path(backend):
    assert backend.cite("nope/1")["found"] is False


def test_runner_strips_image_bytes_from_the_transcript():
    """Bytes cost tokens and prove nothing; the model gets a URL."""
    r = ToolRunner()
    call = r.run("cite", {"path": "core-terms/9/9.2"})
    assert call.ok
    assert "png" not in call.result
    assert call.result["byte_length"] > 1000
    assert call.result["crop_url"] == "/api/crop?path=core-terms%2F9%2F9.2"


# ------------------------------------------------------------------ runner
def test_unknown_tool_is_reported_not_raised():
    r = ToolRunner()
    call = r.run("delete_everything", {})
    assert call.ok is False
    assert "unknown tool" in call.error


def test_tool_failure_is_data_not_a_crash():
    r = ToolRunner()
    call = r.run("follow_references", {"path": "core-terms/9/9.2", "direction": "sideways"})
    assert call.ok is False
    assert "direction" in call.error


def test_call_budget_is_bounded():
    r = ToolRunner()
    for _ in range(ui_config.MAX_TOOL_CALLS):
        r.run("get_provision", {"path": "core-terms/9/9.2"})
    assert r.exhausted is True
