"""Every node and edge the graph holds, built from the stage outputs.

SPEC 2.5 is the contract. Every node carries `:Node` plus a secondary label
from its kind; the referents outside the tree are `:Term`, `:Legislation` and
`:Concept`. The edge list is exactly `CONTAINS`, `NEXT`, `RESOLVES_TO`,
`CANDIDATE`, `USES_TERM`, `DEFINED_IN`, `ABOUT`, `DEFINED_USING`, `CONCEPT_REL`,
`ASSOCIATED_TERM`, `SUPERSEDES`, and every row is batch tagged.

Two encoding decisions worth stating.

`bboxes_own` and `bboxes_extent` are lists of maps, which a Neo4j property
cannot hold, so they are stored as JSON strings. `chat/backends/neo4j_backend.py`
already decodes that encoding first of the three it accepts, so the chat UI
reads them without a change.

A ref's `candidates` are stored the same way *and* emitted as `CANDIDATE` edges,
but only to candidates that exist as nodes. A candidate path for a part that has
not been ingested is a string, and MERGEing an edge to it would mint the very
node SPEC 2.2 forbids refs from minting. The property keeps the full list, so
nothing is lost.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from pipeline.schemas import Concept, DefinitionSite, GraphEdge, Legislation, Node, TermUse

KIND_LABELS = {
    "document": "Document", "part": "Part", "heading": "Heading",
    "preamble": "Preamble", "clause": "Clause", "subclause": "Subclause",
    "item": "Item", "intro": "Intro", "form_row": "FormRow", "table": "Table",
    "cell": "Cell", "ref": "Ref",
}
REFERENT_LABELS = ("Term", "Legislation", "Concept")
# Node fields that are not Neo4j-storable as they stand.
_JSON_PROPS = ("bboxes_own", "bboxes_extent", "candidates")
_DROP = ("children",)


@dataclass
class NodeRow:
    labels: list[str]
    key_field: str                  # id | name | key
    key_value: str
    props: dict
    batch_id: Optional[str]

    def as_dict(self) -> dict:
        return {"labels": self.labels, "key_field": self.key_field,
                "key_value": self.key_value, "props": self.props,
                "batch_id": self.batch_id}


@dataclass
class Rows:
    nodes: list[NodeRow] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)

    def counts(self) -> dict:
        by_label: dict[str, int] = {}
        for row in self.nodes:
            for label in row.labels:
                by_label[label] = by_label.get(label, 0) + 1
        by_type: dict[str, int] = {}
        for edge in self.edges:
            by_type[edge.type] = by_type.get(edge.type, 0) + 1
        return {"nodes": len(self.nodes), "edges": len(self.edges),
                "nodes_by_label": dict(sorted(by_label.items())),
                "edges_by_type": dict(sorted(by_type.items()))}


def walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from walk(child)


def node_props(node: Node) -> dict:
    """A Node as Neo4j properties: JSON strings where a list of maps would be."""
    raw = node.model_dump(mode="json", exclude_none=True)
    props = {k: v for k, v in raw.items() if k not in _DROP}
    for key in _JSON_PROPS:
        value = raw.get(key)
        if value:
            props[key] = json.dumps(value, ensure_ascii=False)
        else:
            props.pop(key, None)
    if node.char_span:
        props["char_span"] = list(node.char_span)
    return props


def load_id_for(batch_id: str, rows: "Rows") -> str:
    """A content hash of exactly what this load asserts.

    Deterministic, so an identical rerun computes the same id and the sweep
    finds nothing stale; different the moment the asserted set changes, which is
    what lets the sweep converge a rerun of the same batch. Hashing the keys
    rather than the whole payload keeps it stable against property-only edits
    that do not change what exists.
    """
    material = "\n".join([batch_id]
                          + sorted(f"N:{r.key_field}={r.key_value}" for r in rows.nodes)
                          + sorted(f"E:{merge_key(e)}" for e in rows.edges))
    return hashlib.sha1(material.encode()).hexdigest()


def node_row(node: Node, *, batch_id: str, access_label: Optional[str] = None,
             load_id: str = "", extra: Optional[dict] = None) -> NodeRow:
    """`batch_id` is the batch of the load that asserted this row.

    That is what `rollback` and `sweep` key on: a node still wearing an older
    tag inside this batch's scope is one this run did not re-assert. Where the
    stage output carried a different tag of its own, it is kept as
    `source_batch_id` so the provenance of the parse is not overwritten by the
    provenance of the load.
    """
    if not batch_id:
        # A row with no batch tag is invisible to both rollback and sweep, so it
        # would live in the graph forever with nothing able to remove it.
        raise ValueError(f"refusing to load {node.path!r} with no batch_id")
    props = node_props(node)
    if node.batch_id and node.batch_id != batch_id:
        props["source_batch_id"] = node.batch_id
    props["batch_id"] = batch_id
    if load_id:
        props["load_id"] = load_id
    if access_label and not props.get("access_label"):
        # DESIGN 5: every node carries the access classification inherited from
        # its document, so role-based access is a filter the query layer applies.
        props["access_label"] = access_label
    if extra:
        props.update(extra)
    return NodeRow(labels=["Node", KIND_LABELS.get(node.kind, "Node")],
                   key_field="id", key_value=node.id, props=props, batch_id=batch_id)


def edge(type_: str, src: str, dst: str, batch_id: str, **props) -> GraphEdge:
    return GraphEdge(type=type_, src=src, dst=dst,
                     props={k: v for k, v in props.items() if v is not None},
                     batch_id=batch_id)


# --------------------------------------------------------------------------
# tier 1, the tree
# --------------------------------------------------------------------------
def tree_rows(trees: dict[str, Node], refs: dict[str, list[Node]], *, batch_id: str,
              document: Optional[Node], access_label: Optional[str] = None,
              load_id: str = "") -> Rows:
    rows = Rows()
    by_path: dict[str, Node] = {}
    for part in sorted(trees):
        for node in walk(trees[part]):
            by_path[node.path] = node

    if document is not None:
        rows.nodes.append(node_row(document, batch_id=batch_id,
                                   access_label=access_label, load_id=load_id))

    for part in sorted(trees):
        root = trees[part]
        if document is not None:
            rows.edges.append(edge("CONTAINS", document.id, root.id, batch_id))
        for node in walk(root):
            rows.nodes.append(node_row(node, batch_id=batch_id,
                                       access_label=access_label, load_id=load_id))
            anatomy = [c for c in node.children if c.kind != "ref"]
            for child in anatomy:
                rows.edges.append(edge("CONTAINS", node.id, child.id, batch_id))
            for left, right in zip(anatomy, anatomy[1:]):
                rows.edges.append(edge("NEXT", left.id, right.id, batch_id))

    # Refs arrive flat (SPEC 2.2) and attach to their parent by path.
    for part in sorted(refs):
        ordered = sorted(refs[part], key=lambda r: (r.order, r.path))
        for ref in ordered:
            parent_path = ref.path.rsplit("/ref@", 1)[0]
            parent = by_path.get(parent_path)
            if parent is None:
                rows.notes.append({"kind": "ref_without_parent", "path": ref.path,
                                   "detail": f"no node at {parent_path}"})
                continue
            rows.nodes.append(node_row(ref, batch_id=batch_id,
                                       access_label=access_label, load_id=load_id))
            rows.edges.append(edge("CONTAINS", parent.id, ref.id, batch_id))
            if ref.target_path and ref.status == "resolved":
                target = by_path.get(ref.target_path)
                if target is not None:
                    rows.edges.append(edge("RESOLVES_TO", ref.id, target.id, batch_id,
                                           scope_rule=ref.scope_rule,
                                           resolver=ref.resolver))
                else:
                    rows.notes.append({"kind": "resolved_target_missing",
                                       "path": ref.path, "target": ref.target_path})
            elif ref.target_path and ref.status == "external":
                rows.edges.append(edge("RESOLVES_TO", ref.id, ref.target_path, batch_id,
                                       scope_rule=ref.scope_rule,
                                       resolver=ref.resolver, external=True))
            for candidate in ref.candidates:
                target = by_path.get(candidate.path)
                if target is None:
                    continue      # a candidate is a string until its part arrives
                rows.edges.append(edge("CANDIDATE", ref.id, target.id, batch_id,
                                       score=candidate.score, reason=candidate.reason))
    return rows


# --------------------------------------------------------------------------
# referents
# --------------------------------------------------------------------------
def legislation_rows(refs: dict[str, list[Node]], records: Iterable[Legislation],
                     *, batch_id: str, load_id: str = "") -> Rows:
    """One Legislation node per normalised key, plus the ref's RESOLVES_TO."""
    rows = Rows()
    known = {r.key: r for r in records}
    cited = {r.target_path for part in refs for r in refs[part]
             if r.ref_kind == "legislation" and r.target_path}
    for key in sorted(cited):
        record = known.get(key) or _legislation_from_key(key)
        rows.nodes.append(NodeRow(labels=["Legislation"], key_field="key",
                                  key_value=record.key,
                                  props={**record.model_dump(exclude_none=True),
                                         "batch_id": batch_id,
                                         **({"load_id": load_id} if load_id else {}),
                                         "year_unknown": record.year == 0},
                                  batch_id=batch_id))
    return rows


def _legislation_from_key(key: str) -> Legislation:
    """A key with no record behind it still names a statute; never invented, and
    the fields it cannot know stay obviously derived."""
    stem = key.split("/", 1)[1] if "/" in key else key
    provision = None
    if stem.count("/") >= 2:
        stem, unit, number = stem.split("/", 2)
        provision = f"{unit}/{number}"
    bits = stem.split("-")
    year = int(bits[-1]) if bits and bits[-1].isdigit() and len(bits[-1]) == 4 else 0
    title = " ".join(w.title() for w in (bits[:-1] if year else bits))
    kind = ("regulations" if "regulations" in stem else
            "eu_regulation" if stem.startswith("regulation-eu") else "act")
    # `Legislation.year` is a required int in schemas.py, so a key with no year
    # in it has to carry 0. Nothing may read that as the year AD 0, so the row
    # is flagged; see the handover note asking for Optional[int].
    return Legislation(key=key, title=title or stem, year=year,
                       instrument_kind=kind, provision=provision)


def term_rows(sites: list[DefinitionSite], uses: list[TermUse],
              nodes_by_id: dict[str, Node], *, batch_id: str,
              load_id: str = "") -> Rows:
    """Term nodes, DEFINED_IN, USES_TERM, and the vocabulary's own DEFINED_USING.

    `DEFINED_USING` is deterministic and falls out of stage 4's own outputs: a
    term used inside another term's defining provision. SPEC 2.3 specifies the
    edge; stage 4 emits uses and sites rather than edges, and every edge in this
    graph is emitted here, so the join happens here like `ASSOCIATED_TERM`.
    """
    rows = Rows()
    aliases: dict[str, set[str]] = {}
    for site in sites:
        aliases.setdefault(site.term, set()).update(site.aliases)
    for use in uses:
        aliases.setdefault(use.term, set())

    for term in sorted(aliases):
        rows.nodes.append(NodeRow(labels=["Term"], key_field="name", key_value=term,
                                  props={"name": term,
                                         "aliases": sorted(aliases[term]),
                                         "batch_id": batch_id,
                                         **({"load_id": load_id} if load_id else {})},
                                  batch_id=batch_id))
    for site in sites:
        if site.definition_node_id not in nodes_by_id:
            rows.notes.append({"kind": "definition_site_without_node",
                               "term": site.term, "node_id": site.definition_node_id})
            continue
        rows.edges.append(edge("DEFINED_IN", site.term, site.definition_node_id,
                               batch_id, scope=site.scope, source=site.source,
                               pointer=site.pointer,
                               aliases=sorted(site.aliases) or None))
    for use in uses:
        if use.node_id not in nodes_by_id:
            rows.notes.append({"kind": "term_use_without_node", "term": use.term,
                               "node_id": use.node_id})
            continue
        rows.edges.append(edge("USES_TERM", use.node_id, use.term, batch_id,
                               char_span=list(use.char_span), status=use.status,
                               ambiguity_kind=use.ambiguity_kind, method=use.method,
                               definition_used=use.definition_used))

    definition_nodes = {site.definition_node_id: site.term for site in sites}
    for use in uses:
        defined_term = definition_nodes.get(use.node_id)
        if defined_term and defined_term != use.term:
            rows.edges.append(edge("DEFINED_USING", defined_term, use.term, batch_id))
    return rows


def concept_rows(concepts: list[Concept], nodes_by_id: dict[str, Node], *,
                 batch_id: str, load_id: str = "") -> Rows:
    rows = Rows()
    known = {c.id for c in concepts}
    for concept in concepts:
        rows.nodes.append(NodeRow(labels=["Concept"], key_field="id",
                                  key_value=concept.id,
                                  props={"id": concept.id, "label": concept.label,
                                         "scope_path": concept.scope_path,
                                         "llm_derived": concept.llm_derived,
                                         "confidence": concept.confidence,
                                         "citable": False, "batch_id": batch_id,
                                         **({"load_id": load_id} if load_id else {})},
                                  batch_id=batch_id))
        for node_id in concept.member_node_ids:
            if node_id not in nodes_by_id:
                rows.notes.append({"kind": "concept_member_without_node",
                                   "concept": concept.id, "node_id": node_id})
                continue
            rows.edges.append(edge("ABOUT", node_id, concept.id, batch_id))
        for relation in concept.relations:
            if relation.dst not in known or relation.src not in known:
                rows.notes.append({"kind": "concept_relation_dangling",
                                   "concept": concept.id, "relation": relation.label,
                                   "src": relation.src, "dst": relation.dst})
                continue
            rows.edges.append(edge("CONCEPT_REL", relation.src, relation.dst, batch_id,
                                   label=relation.label))
    return rows


def dedupe(rows: Rows) -> tuple[Rows, list[dict]]:
    """One row per MERGE key, with everything collapsed written to the audit.

    The MERGE key is type plus endpoints plus the discriminating prop where
    several edges legally join one pair, which SPEC 2.5 says is `char_span` for
    `USES_TERM` and nothing else. Two rows sharing a key are a real event, not
    noise: a rerun that grew them silently is exactly what the audit log exists
    to make findable.
    """
    seen_nodes: dict[tuple[str, str], NodeRow] = {}
    collapsed: list[dict] = []
    out = Rows(notes=list(rows.notes))
    for row in rows.nodes:
        key = (row.key_field, row.key_value)
        if key in seen_nodes:
            collapsed.append({"kind": "node", "key": row.key_value,
                              "labels": row.labels,
                              "reason": "two rows for one node key"})
            seen_nodes[key].props.update(row.props)
            continue
        seen_nodes[key] = row
        out.nodes.append(row)
    seen_edges: dict[tuple, GraphEdge] = {}
    for e in rows.edges:
        key = merge_key(e)
        if key in seen_edges:
            existing = seen_edges[key]
            different = {k: (existing.props.get(k), v) for k, v in e.props.items()
                         if existing.props.get(k) != v}
            collapsed.append({"kind": "edge", "type": e.type, "src": e.src,
                              "dst": e.dst, "differing_props": different,
                              "reason": "two edges share one MERGE key"})
            existing.props.update(e.props)
            continue
        seen_edges[key] = e
        out.edges.append(e)
    return out, collapsed


def dangling_endpoints(rows: Rows) -> list[dict]:
    """Edges whose endpoint has no node row, which the two sinks disagree about.

    NetworkX invents a node for an unknown endpoint; the Neo4j load MATCHes both
    ends and silently writes nothing. Either way the graph would quietly differ
    from the export, so the loader names them instead of letting one of its two
    outputs lie.
    """
    known = {row.key_value for row in rows.nodes}
    out = []
    for e in rows.edges:
        for side, value in (("src", e.src), ("dst", e.dst)):
            if value not in known:
                out.append({"type": e.type, "side": side, "key": value,
                            "src": e.src, "dst": e.dst})
    return out


def merge_key(e: GraphEdge) -> tuple:
    if e.type == "USES_TERM":
        return (e.type, e.src, e.dst, tuple(e.props.get("char_span") or ()))
    return (e.type, e.src, e.dst)
