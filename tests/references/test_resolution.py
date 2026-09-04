"""Resolution: the document's own scope rules, and what it refuses to do.

JS1 1.3.8 (Clauses and Schedules mean the Core Terms'; parts, paragraphs,
annexes and tables in a Schedule mean that Schedule's), 1.3.9 (Paragraphs mean
the appropriate Schedule's), the title parenthetical, the mislabelled
"Clause 1.x inside a Schedule" case, and the rule that outranks all of them:
a ref never mints its target.
"""
from __future__ import annotations

import pytest

from pipeline.references.build import infer_identity, ref_node, span_intact
from pipeline.references.corpus import Corpus
from pipeline.references.detect import detect_part
from pipeline.references.resolve import resolve_pointer


@pytest.fixture
def corpus(core_terms, joint_schedule_1) -> Corpus:
    return Corpus.from_trees({"core-terms": core_terms,
                              "joint-schedule-1": joint_schedule_1})


def resolved_refs(part_id, tree, corpus, identity) -> dict:
    """Every ref in one part, keyed by the pointing words."""
    found = detect_part(part_id, tree)
    out = {}
    for order, pointer in enumerate(found.pointers):
        parent = corpus.node(pointer.parent_path)
        resolution = resolve_pointer(corpus, pointer)
        ref = ref_node(pointer, resolution, parent, identity, order=order, batch_id="B1")
        assert span_intact(ref, parent), ref.path
        out.setdefault(ref.text, []).append(ref)
    return out


def test_a_clause_in_core_terms_resolves_by_js1_1_3_8(core_terms, corpus, identity):
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Clause 3.1"][0]
    assert (ref.status, ref.scope_rule, ref.resolver) == ("resolved", "js1_1.3.8", "scope")
    assert ref.target_path == "core-terms/3/3.1"
    assert ref.confidence is None, "a deterministic resolver never asserts a confidence"


def test_the_mislabelled_clause_inside_a_schedule_is_ambiguous_with_both_candidates(
        joint_schedule_1, corpus, identity):
    """SPEC 4: the pack writes "Clause 1.x" inside Schedules three times. Those
    resolve to the stipulated target, carry status ambiguous with both
    candidates and the reason, and go to review. Never silently, never dropped."""
    ref = resolved_refs("joint-schedule-1", joint_schedule_1, corpus, identity)["Clause 1.2"][0]
    assert ref.status == "ambiguous"
    assert ref.target_path == "core-terms/1/1.2", "the stipulated reading is still stamped"
    assert {c.path for c in ref.candidates} == {"core-terms/1/1.2", "joint-schedule-1/1/1.2"}
    stipulated = next(c for c in ref.candidates if c.path.startswith("core-terms"))
    local = next(c for c in ref.candidates if c.path.startswith("joint-schedule-1"))
    assert stipulated.score > local.score
    assert any(a.startswith("mislabelled_cross_reference") for a in ref.anomalies)


def test_a_clause_in_a_schedule_with_no_local_twin_is_simply_resolved(
        joint_schedule_1, corpus, identity):
    joint_schedule_1.children[0].children[0].text = "Clause 3.1.2 applies here."
    ref = resolved_refs("joint-schedule-1", joint_schedule_1, corpus, identity)["Clause 3.1.2"][0]
    assert (ref.status, ref.target_path) == ("resolved", "core-terms/3/3.1/3.1.2")


def test_a_paragraph_inside_a_schedule_resolves_by_js1_1_3_9(joint_schedule_1, corpus, identity):
    ref = resolved_refs("joint-schedule-1", joint_schedule_1, corpus, identity)["paragraph 2.1"][0]
    assert (ref.status, ref.scope_rule) == ("resolved", "js1_1.3.9")
    assert ref.target_path == "joint-schedule-1/2/2.1"


def test_an_annex_inside_a_schedule_resolves_to_that_schedules_annex(
        joint_schedule_1, corpus, identity):
    ref = resolved_refs("joint-schedule-1", joint_schedule_1, corpus, identity)["Annex 1"][0]
    assert (ref.status, ref.scope_rule) == ("resolved", "js1_1.3.8")
    assert ref.target_path == "joint-schedule-1/Annex 1"


def test_a_paragraph_cited_from_core_terms_names_no_schedule_so_stays_ambiguous(
        core_terms, corpus, identity):
    """"paragraph 2.1 of Joint Schedule 1" names its schedule; a bare one would
    not, and JS1 1.3.9 has nothing to point at."""
    core_terms.children[2].text = "See paragraph 2.1 for detail."   # drops the tail
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["paragraph 2.1"][0]
    assert ref.status == "ambiguous"
    assert ref.target_path is None
    assert any("names none" in a for a in ref.anomalies)


def test_a_named_scope_tail_sends_a_paragraph_to_its_schedule(core_terms, corpus, identity):
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["paragraph 2.1"][0]
    assert (ref.status, ref.target_path) == ("resolved", "joint-schedule-1/2/2.1")


def test_a_bare_schedule_number_is_ambiguous_across_the_three_families(core_terms, corpus, identity):
    core_terms.children[3].text = "See Schedule 2 for detail."
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Schedule 2"][0]
    assert ref.status == "ambiguous"
    assert {c.path for c in ref.candidates} == {"framework-schedule-2", "joint-schedule-2",
                                                "call-off-schedule-2"}
    assert ref.target_path is None


def test_a_citation_to_a_part_that_has_not_arrived_stays_unresolved(core_terms, corpus, identity):
    """Refs never mint target nodes: the conventional id is kept as a candidate
    string and the ref waits for the part to land."""
    core_terms.children[3].text = "See Framework Schedule 4 (Framework Management)."
    ref = resolved_refs("core-terms", core_terms, corpus, identity)[
        "Framework Schedule 4 (Framework Management)"][0]
    assert ref.status == "unresolved"
    assert ref.target_path is None
    assert [c.path for c in ref.candidates] == ["framework-schedule-4"]
    assert not corpus.exists("framework-schedule-4")


def test_a_title_parenthetical_naming_an_ingested_part_resolves_by_title(
        core_terms, corpus, identity):
    core_terms.children[3].text = "See Schedule 1 (Definitions) for detail."
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Schedule 1 (Definitions)"][0]
    assert (ref.status, ref.scope_rule) == ("resolved", "title_paren")
    assert ref.target_path == "joint-schedule-1"


def test_a_title_parenthetical_can_name_a_part_the_register_knows_of(core_terms, corpus, identity):
    """Once stage 0's part map exists, a title names a part this batch lacks."""
    corpus.register_parts({"call-off-schedule-6": "Call-Off Schedule 6 (Materials)"})
    core_terms.children[3].text = "See Schedule 6 (Materials) for detail."
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Schedule 6 (Materials)"][0]
    assert (ref.status, ref.scope_rule) == ("unresolved", "title_paren")
    assert [c.path for c in ref.candidates] == ["call-off-schedule-6"]


def test_a_citation_to_a_number_that_does_not_exist_offers_its_ancestor(
        core_terms, corpus, identity):
    core_terms.children[3].text = "See Clause 3.1.9 for detail."
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Clause 3.1.9"][0]
    assert ref.status == "unresolved"
    assert [c.path for c in ref.candidates] == ["core-terms/3/3.1"]
    assert "nearest enclosing provision" in ref.candidates[0].reason


def test_anaphora_is_never_resolved_by_the_grammar(core_terms, corpus, identity):
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["this Clause"][0]
    assert ref.status == "ambiguous"
    assert ref.target_path is None
    assert ref.resolver == "scope"
    assert any("anaphoric reference" in a for a in ref.anomalies)


def test_legislation_is_external_and_resolved_by_the_grammar(core_terms, corpus, identity):
    ref = resolved_refs("core-terms", core_terms, corpus, identity)["Bribery Act 2010"][0]
    assert (ref.status, ref.resolver, ref.scope_rule) == ("external", "grammar", "none")
    assert ref.target_path == "legislation/bribery-act-2010"


def test_a_range_mints_one_ref_per_member_with_distinct_ids(core_terms, corpus, identity):
    refs = [r for group in resolved_refs("core-terms", core_terms, corpus, identity).values()
            for r in group if r.ref_kind == "clause" and r.group_id]
    members = {r.target_path or r.text for r in refs}
    assert "core-terms/1" in members and "core-terms/4" in members
    ids = [r.id for r in refs]
    assert len(ids) == len(set(ids)), "range members collided on one id"
    paths = [r.path for r in refs if "+" in r.path]
    assert paths, "an interior range member should carry the disambiguating suffix"


def test_every_member_of_a_list_shares_one_group_id(core_terms, corpus, identity):
    refs = [r for group in resolved_refs("core-terms", core_terms, corpus, identity).values()
            for r in group if r.group_id]
    groups = {r.group_id for r in refs}
    assert len(groups) >= 1
    for group in groups:
        assert len([r for r in refs if r.group_id == group]) > 1


def test_identity_is_derived_from_the_trees_own_ids(core_terms, joint_schedule_1,
                                                    doc_id, version):
    derived = infer_identity([core_terms, joint_schedule_1])
    assert derived.verified is False, "the test document id is not a config candidate"
    derived = infer_identity([core_terms, joint_schedule_1], document=doc_id)
    assert (derived.document, derived.version, derived.verified) == (doc_id, version, True)


def test_a_statute_mention_with_no_year_is_never_keyed_as_external(core_terms, corpus,
                                                                  identity):
    """The pack cites "the Regulations" and "Freedom of Information Act (FOIA)"
    with no year. `legislation/` with nothing after it is not a statute, and
    status external on an empty key would claim a resolution that never
    happened, so these go to review instead."""
    from pipeline.references.detect import Pointer
    from pipeline.references.resolve import resolve_pointer

    pointer = Pointer(parent_path="core-terms/4", part="core-terms", span=(0, 15),
                      text="the Regulations", ref_kind="legislation",
                      unit="legislation", method="llm")
    resolution = resolve_pointer(corpus, pointer)
    assert resolution.status == "unresolved"
    assert resolution.target_path is None
    assert any("legislation_not_normalisable" in n for n in resolution.notes)


def test_an_llm_extracted_statute_that_does_normalise_still_becomes_external(
        core_terms, corpus, identity):
    from pipeline.references.detect import Pointer
    from pipeline.references.resolve import resolve_pointer

    pointer = Pointer(parent_path="core-terms/4", part="core-terms", span=(0, 16),
                      text="Bribery Act 2010", ref_kind="legislation",
                      unit="legislation", method="llm")
    resolution = resolve_pointer(corpus, pointer)
    assert resolution.status == "external"
    assert resolution.target_path == "legislation/bribery-act-2010"


# --------------------------------------------------------------------------
# one span, two refs: the collision the four-part run turned up
# --------------------------------------------------------------------------
def js1_shaped_tree(doc_id, version, texts):
    """A definitions schedule: a two-column table whose cells carry citations.

    Shaped like Joint Schedule 1, which is where the collisions landed, because
    a definition cell is one long text with many citations in it.
    """
    from pipeline.schemas import Node, content_hash, lineage_key, node_id

    def node(path, kind, **kw):
        text = kw.pop("text", None)
        return Node(id=node_id(doc_id, version, path),
                    lineage_key=lineage_key(doc_id, path),
                    content_hash=content_hash(text) if text else None,
                    path=path, kind=kind, text=text, page_start=1, page_end=1, **kw)

    cells = []
    for i, text in enumerate(texts):
        cells.append(node(f"joint-schedule-1/1/table/{i}/1", "cell", text=text,
                          row=i, col=1, cell_role="value", order=2 + i))
    table = node("joint-schedule-1/1/table", "table", n_rows=len(texts), n_cols=2,
                 order=1, children=cells)
    head = node("joint-schedule-1/1", "heading", order=0, children=[table],
                title="Definitions")
    return node("joint-schedule-1", "part", order=0, children=[head],
                title="Joint Schedule 1 (Definitions)", part_family="joint-schedule",
                unit_label="Paragraph", batch_id="B2")


def build_part_refs(tree, corpus, identity, part="joint-schedule-1"):
    from pipeline.references.__main__ import resolve_part
    from pipeline.references.detect import detect_part

    detection = detect_part(part, tree)
    return resolve_part(part, detection, corpus, identity, "B2")


def test_the_same_citation_detected_twice_becomes_one_ref(doc_id, version, identity,
                                                          core_terms):
    """A duplicate of the same target on the same characters is one citation."""
    from pipeline.references.corpus import Corpus
    from pipeline.references.detect import Pointer
    from pipeline.references.__main__ import resolve_part
    from pipeline.references.detect import PartDetection

    tree = js1_shaped_tree(doc_id, version, ["means the thing in Clause 3.1."])
    corpus = Corpus.from_trees({"core-terms": core_terms, "joint-schedule-1": tree})
    detection = PartDetection(part="joint-schedule-1")
    cell = tree.children[0].children[0].children[0]
    for _ in range(2):
        detection.pointers.append(Pointer(
            parent_path=cell.path, part="joint-schedule-1", span=(20, 30),
            text=cell.text[20:30], ref_kind="clause", unit="Clause", number="3.1"))
    refs, _statutes, _ctx, violations = resolve_part(
        "joint-schedule-1", detection, corpus, identity, "B2")
    assert len(refs) == 1, "the same citation of the same target minted two refs"
    notes = [v for v in violations if v["kind"] == "duplicate_citation_deduped"]
    assert len(notes) == 1 and notes[0]["severity"] == "note"


def test_two_different_targets_on_one_span_both_survive(doc_id, version, identity,
                                                        core_terms):
    """Distinct citations sharing characters are both real, so both get ids."""
    from pipeline.references.corpus import Corpus
    from pipeline.references.detect import PartDetection, Pointer
    from pipeline.references.__main__ import resolve_part

    tree = js1_shaped_tree(doc_id, version, ["means the thing in Clause 3.1."])
    corpus = Corpus.from_trees({"core-terms": core_terms, "joint-schedule-1": tree})
    cell = tree.children[0].children[0].children[0]
    detection = PartDetection(part="joint-schedule-1")
    for number in ("3.1", "9.2"):
        detection.pointers.append(Pointer(
            parent_path=cell.path, part="joint-schedule-1", span=(20, 30),
            text=cell.text[20:30], ref_kind="clause", unit="Clause", number=number))
    refs, _statutes, _ctx, violations = resolve_part(
        "joint-schedule-1", detection, corpus, identity, "B2")

    assert len(refs) == 2, "a real second citation was dropped"
    assert len({r.id for r in refs}) == 2, "two refs collided on one id"
    assert len({r.path for r in refs}) == 2
    assert any(p.endswith("+1") for p in (r.path for r in refs))
    assert any("span_shared_with_another_ref" in a
               for r in refs for a in r.anomalies)
    assert not [v for v in violations if v.get("severity") != "note"]


def test_a_deduped_duplicate_does_not_fail_the_run(tmp_path, monkeypatch):
    """A note records something handled correctly; only a real violation fails
    the exit code, or a tidy dedupe would read as a broken stage."""
    from pipeline.references.__main__ import main

    assert main(["--input", "fixtures", "--run", "t", "--no-llm", "--quiet",
                 "--output-dir", str(tmp_path)]) == 0


# --------------------------------------------------------------------------
# drafter-named sub-parts: the audit's finding, SPEC 2.2
# --------------------------------------------------------------------------
def co9_shaped_tree(doc_id, version):
    """Call-Off Schedule 9's shape: Part A's paragraphs sit directly under the
    part, Part B is a heading the drafters named and restarts the numbering."""
    from pipeline.schemas import Node, content_hash, lineage_key, node_id

    def node(path, kind, **kw):
        text = kw.pop("text", None)
        return Node(id=node_id(doc_id, version, path),
                    lineage_key=lineage_key(doc_id, path),
                    content_hash=content_hash(text) if text else None,
                    path=path, kind=kind, text=text, page_start=1, page_end=1, **kw)

    pid = "call-off-schedule-9"
    # Part A, unnamed: its paragraphs hang off the part itself
    a34 = node(f"{pid}/3/3.4", "subclause", label="3.4", order=3,
               text="Part A's own paragraph 3.4.")
    a3 = node(f"{pid}/3", "heading", label="3", title="Security Standards", order=2,
              children=[a34])
    a5 = node(f"{pid}/5", "heading", label="5", title="Security breach", order=4,
              children=[node(f"{pid}/5/5.1", "subclause", label="5.1", order=5,
                             text="Only Part A has a 5.1.")])
    # Part B, named by the drafters, numbering restarts
    b = f"{pid}/part-b-long-form-security-requirements"
    b34 = node(f"{b}/3/3.4", "subclause", label="3.4", order=8,
               text="Part B's own paragraph 3.4, a different obligation.")
    b3 = node(f"{b}/3", "heading", label="3", title="Security Standards", order=7,
              children=[b34])
    b_cite = node(f"{b}/2/2.1", "subclause", label="2.1", order=10,
                  text="The Supplier shall comply with Paragraph 3.4.")
    b_cross = node(f"{b}/2/2.2", "subclause", label="2.2", order=11,
                   text="Nothing in Paragraph 5.1 is affected.")
    b2 = node(f"{b}/2", "heading", label="2", title="Compliance", order=9,
              children=[b_cite, b_cross])
    part_b = node(b, "heading", title="Part B: Long Form Security Requirements",
                  order=6, children=[b2, b3])
    a_cite = node(f"{pid}/2/2.1", "subclause", label="2.1", order=1,
                  text="The Supplier shall comply with Paragraph 3.4.")
    a2 = node(f"{pid}/2", "heading", label="2", title="Complying", order=0,
              children=[a_cite])
    return node(pid, "part", order=0, children=[a2, a3, a5, part_b],
                title="Call-Off Schedule 9", part_family="call-off-schedule",
                unit_label="Paragraph", batch_id="B4")


def test_a_part_b_citation_resolves_inside_part_b(doc_id, version, identity):
    """The audit's exact finding: 17 refs inside Part B citing Paragraph 3.4
    resolved to Part A's 3.4, deterministically and confidently."""
    from pipeline.references.corpus import Corpus

    tree = co9_shaped_tree(doc_id, version)
    corpus = Corpus.from_trees({"call-off-schedule-9": tree})
    refs = resolved_refs("call-off-schedule-9", tree, corpus, identity)
    b = "call-off-schedule-9/part-b-long-form-security-requirements"

    from_b = [r for r in refs["Paragraph 3.4"] if r.path.startswith(b)]
    assert len(from_b) == 1
    ref = from_b[0]
    assert ref.target_path == f"{b}/3/3.4", "resolved outside its own Part"
    assert ref.target_path != "call-off-schedule-9/3/3.4"
    assert (ref.status, ref.scope_rule) == ("resolved", "js1_1.3.9")


def test_a_part_a_citation_still_resolves_inside_part_a(doc_id, version, identity):
    """The same number cited from outside the named division stays outside it."""
    from pipeline.references.corpus import Corpus

    tree = co9_shaped_tree(doc_id, version)
    corpus = Corpus.from_trees({"call-off-schedule-9": tree})
    refs = resolved_refs("call-off-schedule-9", tree, corpus, identity)
    b = "call-off-schedule-9/part-b-long-form-security-requirements"

    from_a = [r for r in refs["Paragraph 3.4"] if not r.path.startswith(b)]
    assert len(from_a) == 1
    assert from_a[0].target_path == "call-off-schedule-9/3/3.4"


def test_a_number_only_outside_the_sub_part_falls_through(doc_id, version, identity):
    """A citation reaching across a Part boundary is ordinary and still
    resolves; it is the ones that could have stayed home that were the bug."""
    from pipeline.references.corpus import Corpus

    tree = co9_shaped_tree(doc_id, version)
    corpus = Corpus.from_trees({"call-off-schedule-9": tree})
    refs = resolved_refs("call-off-schedule-9", tree, corpus, identity)
    ref = refs["Paragraph 5.1"][0]
    assert ref.status == "resolved"
    assert ref.target_path == "call-off-schedule-9/5/5.1"
    assert any(a.startswith("resolved_across_sub_part") for a in ref.anomalies), \
        "a resolution that left its division was not recorded"


def test_the_sub_part_index_reads_the_drafters_own_naming(doc_id, version):
    from pipeline.references.corpus import Corpus

    tree = co9_shaped_tree(doc_id, version)
    info = Corpus.from_trees({"call-off-schedule-9": tree}).parts["call-off-schedule-9"]
    b = "call-off-schedule-9/part-b-long-form-security-requirements"
    assert info.sub_parts == [b]
    assert info.sub_part_for(f"{b}/2/2.1") == b
    assert info.sub_part_for("call-off-schedule-9/3/3.4") is None
    assert info.sub_part_labels[b]["3.4"] == f"{b}/3/3.4"
    assert info.outside_labels["3.4"] == "call-off-schedule-9/3/3.4"


def test_the_named_part_rule_is_shared_with_the_vocabulary_tier():
    """One definition of what a drafter-named Part looks like, so reference
    scoping and definition scoping cannot drift apart."""
    from pipeline.references import corpus as refs_corpus

    assert refs_corpus.NAMED_SUB_PART.match("Part B: Long Form Security Requirements")
    assert refs_corpus.NAMED_SUB_PART.match("Part A")
    assert not refs_corpus.NAMED_SUB_PART.match("Particulars of the Supplier")
    assert not refs_corpus.NAMED_SUB_PART.match("3 Security Standards")
    try:
        from pipeline.vocabulary.declared import NAMED_SUB_PART as theirs
    except Exception:                                     # noqa: BLE001
        return                                            # mirrored, reported in the run
    assert refs_corpus.NAMED_SUB_PART.pattern == theirs.pattern


def test_one_expanded_range_resolves_in_one_namespace(doc_id, version, identity):
    """The eval side's single-line demonstration of the bug.

    Call-Off Schedule 9 Part B cites "3.4 to 3.6". Under the flat part-wide
    reading, member 1 resolved to Part A's 3.4 while members 2 and 3 went to
    Part B's 3.5 and 3.6, because Part A has no 3.5 or 3.6 for them to hit.
    One range, two namespaces, and nothing in the output said so. The
    nearest-sub-part rule lands all three in Part B with no special case.
    """
    from pipeline.references.corpus import Corpus
    from pipeline.schemas import Node, content_hash, lineage_key, node_id

    def node(path, kind, **kw):
        text = kw.pop("text", None)
        return Node(id=node_id(doc_id, version, path),
                    lineage_key=lineage_key(doc_id, path),
                    content_hash=content_hash(text) if text else None,
                    path=path, kind=kind, text=text, page_start=1, page_end=1, **kw)

    pid = "call-off-schedule-9"
    b = f"{pid}/part-b-long-form-security-requirements"
    # Part A has 3.4 and no 3.5 or 3.6, which is what split the range
    part_a = node(f"{pid}/3", "heading", label="3", title="Security Standards", order=1,
                  children=[node(f"{pid}/3/3.4", "subclause", label="3.4", order=2,
                                 text="Part A's 3.4.")])
    b3 = node(f"{b}/3", "heading", label="3", title="Security Standards", order=4,
              children=[node(f"{b}/3/3.{n}", "subclause", label=f"3.{n}", order=4 + n,
                             text=f"Part B's 3.{n}.") for n in (4, 5, 6)])
    citing = node(f"{b}/2/2.1", "subclause", label="2.1", order=8,
                  text="The Supplier shall comply with Paragraphs 3.4 to 3.6.")
    b2 = node(f"{b}/2", "heading", label="2", title="Compliance", order=7,
              children=[citing])
    part_b = node(b, "heading", title="Part B: Long Form Security Requirements",
                  order=3, children=[b2, b3])
    tree = node(pid, "part", order=0, children=[part_a, part_b],
                title="Call-Off Schedule 9", part_family="call-off-schedule",
                unit_label="Paragraph", batch_id="B4")

    corpus = Corpus.from_trees({pid: tree})
    refs = resolved_refs(pid, tree, corpus, identity)
    members = [r for group in refs.values() for r in group
               if r.group_id and r.ref_kind == "paragraph"]

    assert len(members) == 3, "the range did not expand to three members"
    assert len({r.group_id for r in members}) == 1, "one phrase, one group"
    targets = sorted(r.target_path for r in members)
    assert targets == [f"{b}/3/3.4", f"{b}/3/3.5", f"{b}/3/3.6"]
    assert all(r.status == "resolved" for r in members)
    assert f"{pid}/3/3.4" not in targets, "member 1 still crossed into Part A"
    namespaces = {corpus.parts[pid].sub_part_for(r.target_path) for r in members}
    assert namespaces == {b}, f"one range resolved across {len(namespaces)} namespaces"


def test_an_annex_citation_never_lands_on_a_paragraph(doc_id, version, identity):
    """Found checking the sub-part fix. "Annex 2" was resolving onto whatever
    provision happened to be numbered 2, because the label forms for Annex and
    Part ended in a bare number. An Annex the part does not have must stay
    unresolved, not resolve to something else of the same number."""
    from pipeline.references.corpus import Corpus
    from pipeline.schemas import Node, content_hash, lineage_key, node_id

    def node(path, kind, **kw):
        text = kw.pop("text", None)
        return Node(id=node_id(doc_id, version, path),
                    lineage_key=lineage_key(doc_id, path),
                    content_hash=content_hash(text) if text else None,
                    path=path, kind=kind, text=text, page_start=1, page_end=1, **kw)

    pid = "call-off-schedule-9"
    annex = node(f"{pid}/part-b-annex-1-baseline", "heading", order=3,
                 title="Part B - Annex 1: Baseline security requirements",
                 children=[node(f"{pid}/part-b-annex-1-baseline/1", "clause",
                                label="1", order=4, text="Baseline.")])
    two = node(f"{pid}/2", "heading", label="2", title="Security Requirements", order=1,
               children=[node(f"{pid}/2/2.1", "clause", label="2.1", order=2,
                              text="A provision merely numbered 2.")])
    citing = node(f"{pid}/1", "clause", label="1", order=0,
                  text="See Annex 1 and also Annex 2 for detail.")
    tree = node(pid, "part", order=0, children=[citing, two, annex],
                title="Call-Off Schedule 9", part_family="call-off-schedule",
                unit_label="Paragraph", batch_id="B4")

    corpus = Corpus.from_trees({pid: tree})
    refs = resolved_refs(pid, tree, corpus, identity)

    annex_1 = refs["Annex 1"][0]
    assert annex_1.status == "resolved"
    assert annex_1.target_path == f"{pid}/part-b-annex-1-baseline", \
        "Annex 1 did not find the division the drafters named"

    annex_2 = refs["Annex 2"][0]
    assert annex_2.status == "unresolved", "a missing Annex resolved to something"
    assert annex_2.target_path is None
    assert annex_2.target_path != f"{pid}/2"
    assert any("names no such division" in a for a in annex_2.anomalies)
