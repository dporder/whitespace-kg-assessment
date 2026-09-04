"""The Neo4j half of stage 7. MERGE only, batch tagged, reversible.

SPEC 2.5: "Uniqueness constraints collapse to `Node.id`, plus `Term.name`,
`Legislation.key`, `Concept.id`, and a non unique index on `Node.lineage_key`
and on `Node.label`... MERGE only, never CREATE on possibly existing keys, and
every relationship MERGE needs an explicit key or a rerun grows parallel edges.
Every node and edge gets `batch_id`. Three functions must exist and be tested.
`rollback(batch_id)` removes a batch completely. `sweep(scope, batch_id)`
deletes anything in scope carrying an earlier batch tag this batch did not re
assert, which is what makes a rerun converge on state rather than only avoiding
duplicates."

The sweep keys on a `load_id`, a content hash of exactly what one load
asserted, rather than on batch identity alone. Batch identity cannot see a
rerun of the same batch, so a second load of B1 that no longer asserts a clause
would leave it in the graph wearing the same tag as everything else. An
identical rerun hashes to the same load id and therefore sweeps nothing, which
keeps the deterministic guarantee intact.

Every query here is a module-level constant with $parameters. Relationship types
and labels cannot be parameterised in Cypher, so the few places they appear are
interpolated from this module's own constants, never from input: `_TYPE` and
`_LABEL` reject anything that is not one of the names SPEC 2.5 lists.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

import config
from pipeline.schemas import GraphEdge

from .rows import KIND_LABELS, NodeRow, REFERENT_LABELS, merge_key

EDGE_TYPES = ("CONTAINS", "NEXT", "RESOLVES_TO", "CANDIDATE", "USES_TERM",
              "DEFINED_IN", "ABOUT", "DEFINED_USING", "CONCEPT_REL",
              "ASSOCIATED_TERM", "SUPERSEDES")
ALLOWED_LABELS = {"Node", *KIND_LABELS.values(), *REFERENT_LABELS}
KEY_FIELDS = {"id", "name", "key", "path"}
_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

CONSTRAINTS = [
    "CREATE CONSTRAINT node_id IF NOT EXISTS FOR (n:Node) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT term_name IF NOT EXISTS FOR (t:Term) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT legislation_key IF NOT EXISTS FOR (l:Legislation) "
    "REQUIRE l.key IS UNIQUE",
    "CREATE CONSTRAINT concept_id IF NOT EXISTS FOR (c:Concept) REQUIRE c.id IS UNIQUE",
]
INDEXES = [
    "CREATE INDEX node_lineage_key IF NOT EXISTS FOR (n:Node) ON (n.lineage_key)",
    "CREATE INDEX node_label IF NOT EXISTS FOR (n:Node) ON (n.label)",
    "CREATE INDEX node_path IF NOT EXISTS FOR (n:Node) ON (n.path)",
    "CREATE INDEX node_batch IF NOT EXISTS FOR (n:Node) ON (n.batch_id)",
]

Q_COUNT_NODES = "MATCH (n) WHERE n.batch_id = $batch RETURN count(n) AS n"
Q_COUNT_RELS = "MATCH ()-[r]->() WHERE r.batch_id = $batch RETURN count(r) AS n"
Q_ALL_NODES = "MATCH (n) RETURN count(n) AS n"
Q_ALL_RELS = "MATCH ()-[r]->() RETURN count(r) AS n"

# rollback: a batch leaves nothing behind, relationships included
Q_ROLLBACK_RELS = """
MATCH ()-[r]->() WHERE r.batch_id = $batch
WITH r LIMIT $limit
DELETE r
RETURN count(*) AS n
"""
# A referent is shared infrastructure: `Term.name`, `Legislation.key` and
# `Concept.id` are global keys precisely so that fifty mentions of one statute
# meet at one node. So the batch that last asserted a referent is not its owner,
# and rolling that batch back must not delete a node another batch is still
# using. Its own relationships are gone by this point, so anything still
# attached belongs to someone else.
Q_ROLLBACK_NODES = """
MATCH (n) WHERE n.batch_id = $batch
  AND NOT ((n:Term OR n:Legislation OR n:Concept) AND EXISTS { (n)--() })
WITH n LIMIT $limit
DETACH DELETE n
RETURN count(*) AS n
"""
Q_ROLLBACK_KEPT_REFERENTS = """
MATCH (n) WHERE n.batch_id = $batch
  AND (n:Term OR n:Legislation OR n:Concept) AND EXISTS { (n)--() }
RETURN coalesce(n.name, n.key, n.id) AS key, labels(n) AS labels,
       n.first_batch_id AS first_batch_id, count { (n)--() } AS remaining
"""

# The sweep keys on `load_id`, not on `batch_id`. Batch identity cannot see a
# rerun of the same batch: a second load of B1 that no longer asserts a clause
# leaves it sitting there wearing "B1", indistinguishable from the rows the
# rerun did assert, so the graph never converges. `load_id` is a content hash of
# exactly what this load asserted, so "anything in scope this load did not
# assert" is expressible, and an identical rerun computes the same id and
# therefore sweeps nothing.
Q_SWEEP_NODE_IDS = """
MATCH (n:Node)
WHERE coalesce(n.load_id, '') <> $load_id
  AND any(prefix IN $scope WHERE n.path = prefix OR n.path STARTS WITH prefix + '/')
RETURN n.id AS id, n.path AS path, n.batch_id AS batch_id, n.load_id AS load_id
"""
Q_SWEEP_NODES = """
MATCH (n:Node)
WHERE coalesce(n.load_id, '') <> $load_id
  AND any(prefix IN $scope WHERE n.path = prefix OR n.path STARTS WITH prefix + '/')
DETACH DELETE n
RETURN count(*) AS n
"""
Q_SWEEP_RELS = """
MATCH (s:Node)-[r]->()
WHERE coalesce(r.load_id, '') <> $load_id
  AND any(prefix IN $scope WHERE s.path = prefix OR s.path STARTS WITH prefix + '/')
DELETE r
RETURN count(*) AS n
"""
# Orphan referents, scoped to the ones THIS load asserted. The unscoped version
# of these two queries was a live hazard: a Concept loaded for B2 whose member
# provisions live in B4 sits edgeless until B4 arrives, and an unrelated B1
# sweep would have deleted it. A referent is only ever cleaned up by the load
# that just claimed it, and only when nothing at all points at it.
Q_ORPHAN_REFERENTS = """
UNWIND $keys AS key
MATCH (n) WHERE (n:Term OR n:Legislation OR n:Concept)
  AND coalesce(n.name, n.key, n.id) = key
  AND n.load_id = $load_id
  AND NOT (n)--()
RETURN labels(n) AS labels, key AS key, n.batch_id AS batch_id
"""
Q_DELETE_ORPHAN_REFERENTS = """
UNWIND $keys AS key
MATCH (n) WHERE (n:Term OR n:Legislation OR n:Concept)
  AND coalesce(n.name, n.key, n.id) = key
  AND n.load_id = $load_id
  AND NOT (n)--()
DELETE n
RETURN count(*) AS n
"""

# Salience recomputed FROM THE GRAPH, so it covers every batch loaded so far
# rather than only the one in hand. This is what makes the incremental
# demonstration mean anything: a Core Terms clause cited from Joint Schedule 1
# gains breadth the moment B2 lands, which a per-batch computation over stage
# outputs could never see.
Q_FURNITURE = """
MATCH (n:Node) WHERE n.content_hash IS NOT NULL AND n.text IS NOT NULL
WITH n.content_hash AS hash, collect(n.id) AS ids,
     count(n) AS repeats, count(DISTINCT split(n.path, '/')[0]) AS parts
WHERE repeats >= $min_repeats AND parts >= $min_parts
UNWIND ids AS id
RETURN id AS id, 'repeated_across_parts' AS reason
UNION
MATCH (n:Node) WHERE n.text =~ '^\\\\s*\\\\[[^\\\\]]{0,80}\\\\]\\\\s*$'
RETURN n.id AS id, 'form_placeholder' AS reason
"""
Q_ZERO_SALIENCE = """
MATCH (n:Node) SET n.salience = 0.0, n.salience_flagged = false,
                  n.salience_flag_reason = null
RETURN count(*) AS n
"""
Q_ZERO_TERM_SALIENCE = """
MATCH (t:Term) SET t.salience = 0.0, t.salience_flagged = false,
                  t.salience_flag_reason = null
RETURN count(*) AS n
"""
Q_NODE_CITATIONS = """
MATCH (p:Node)-[:CONTAINS]->(r:Node)-[:RESOLVES_TO]->(t:Node)
WHERE r.kind = 'ref' AND NOT p.id IN $furniture
RETURN t.id AS id, count(r) AS frequency,
       count(DISTINCT split(r.path, '/')[0]) AS breadth, t.kind AS kind
"""
Q_TERM_USES = """
MATCH (n:Node)-[u:USES_TERM]->(t:Term)
WHERE NOT n.id IN $furniture
RETURN t.name AS name, count(u) AS frequency,
       count(DISTINCT split(n.path, '/')[0]) AS breadth
"""

Q_SET_NODE_SALIENCE = """
UNWIND $rows AS row
MATCH (n:Node {id: row.id})
SET n.salience = row.salience,
    n.salience_flagged = row.flagged,
    n.salience_flag_reason = row.reason
RETURN count(*) AS n
"""
Q_SET_TERM_SALIENCE = """
UNWIND $rows AS row
MATCH (t:Term {name: row.name})
SET t.salience = row.salience,
    t.salience_flagged = row.flagged,
    t.salience_flag_reason = row.reason
RETURN count(*) AS n
"""


def _label(name: str) -> str:
    if name not in ALLOWED_LABELS or not _SAFE.match(name):
        raise ValueError(f"refusing to build Cypher with label {name!r}")
    return name


def _type(name: str) -> str:
    if name not in EDGE_TYPES or not _SAFE.match(name):
        raise ValueError(f"refusing to build Cypher with relationship type {name!r}")
    return name


def _key_field(name: str) -> str:
    if name not in KEY_FIELDS:
        raise ValueError(f"refusing to build Cypher with key field {name!r}")
    return name


def node_merge(labels: list[str], key_field: str) -> str:
    """MERGE on the uniqueness key, then set the rest. Never CREATE."""
    head = _label(labels[0])
    extra = "".join(f":{_label(x)}" for x in labels[1:])
    field = _key_field(key_field)
    # `SET n:Extra, n += props` when there is a secondary label, `SET n += props`
    # when there is not: `SET n, n += props` is a syntax error.
    setter = (f"SET n{extra}, n += row.props" if extra else "SET n += row.props")
    # `batch_id` is the batch that last asserted the row, which is what the
    # sweep needs: a node still wearing an older tag was not re-asserted.
    # `first_batch_id` is the batch that introduced it, written once, so the
    # difference between "this batch created it" and "this batch confirmed it"
    # survives. See the handover note on rollback semantics.
    return (f"UNWIND $rows AS row\n"
            f"MERGE (n:{head} {{{field}: row.key}})\n"
            f"ON CREATE SET n.first_batch_id = row.props.batch_id\n"
            f"{setter}\n"
            f"RETURN count(*) AS n")


# Which label and key field each end of an edge is addressed by. The old
# `MATCH (a) WHERE a.id = $x OR a.name = $x OR a.key = $x` scanned every node in
# the graph for both ends of every edge, and matched on any of three properties,
# so a Term whose name happened to equal a node id would have joined the wrong
# thing. RESOLVES_TO is the one type with two possible destinations, split below
# by whether the target is a legislation key.
ENDPOINTS = {
    "CONTAINS": ("Node", "id", "Node", "id"),
    "NEXT": ("Node", "id", "Node", "id"),
    "CANDIDATE": ("Node", "id", "Node", "id"),
    "SUPERSEDES": ("Node", "id", "Node", "id"),
    "RESOLVES_TO": ("Node", "id", "Node", "id"),
    "RESOLVES_TO_LEGISLATION": ("Node", "id", "Legislation", "key"),
    # A ref resolving into a part this load does not hold: the target's id
    # cannot be computed here, because ids are minted per part under that
    # part's own template version, so the edge is addressed by the target's
    # path instead and written as soon as that part is in the graph.
    "RESOLVES_TO_BY_PATH": ("Node", "id", "Node", "path"),
    "USES_TERM": ("Node", "id", "Term", "name"),
    "DEFINED_IN": ("Term", "name", "Node", "id"),
    "DEFINED_USING": ("Term", "name", "Term", "name"),
    "ABOUT": ("Node", "id", "Concept", "id"),
    "CONCEPT_REL": ("Concept", "id", "Concept", "id"),
    "ASSOCIATED_TERM": ("Concept", "id", "Term", "name"),
}


def edge_bucket(e: GraphEdge) -> str:
    """The ENDPOINTS bucket an edge belongs to."""
    if e.type == "RESOLVES_TO":
        if str(e.dst).startswith("legislation/"):
            return "RESOLVES_TO_LEGISLATION"
        if e.props.get("target_is_path"):
            return "RESOLVES_TO_BY_PATH"
    return e.type


def edge_merge(bucket: str, discriminated: bool) -> str:
    """MERGE with an explicit key: a rerun updates, it never grows a twin."""
    rel = _type("RESOLVES_TO" if bucket.startswith("RESOLVES_TO") else bucket)
    src_label, src_key, dst_label, dst_key = ENDPOINTS[bucket]
    key = (" {char_span: row.char_span}" if discriminated else "")
    return (f"UNWIND $rows AS row\n"
            f"MATCH (a:{_label(src_label)} {{{_key_field(src_key)}: row.src}})\n"
            f"MATCH (b:{_label(dst_label)} {{{_key_field(dst_key)}: row.dst}})\n"
            f"MERGE (a)-[r:{rel}{key}]->(b)\n"
            f"ON CREATE SET r.first_batch_id = row.batch_id\n"
            f"SET r += row.props, r.batch_id = row.batch_id, r.load_id = row.load_id\n"
            f"RETURN count(*) AS n")


def endpoint_lookup(bucket: str) -> str:
    """Which of these endpoint keys the graph already holds."""
    src_label, src_key, dst_label, dst_key = ENDPOINTS[bucket]
    return (f"UNWIND $src AS key MATCH (n:{_label(src_label)} "
            f"{{{_key_field(src_key)}: key}}) RETURN 'src' AS side, key AS key\n"
            f"UNION\n"
            f"UNWIND $dst AS key MATCH (n:{_label(dst_label)} "
            f"{{{_key_field(dst_key)}: key}}) RETURN 'dst' AS side, key AS key")


def password() -> Optional[str]:
    pw = os.environ.get("NEO4J_PASSWORD")
    if pw:
        return pw
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    if config.ENV_FILE.exists():
        return (dotenv_values(config.ENV_FILE) or {}).get("NEO4J_PASSWORD")
    return None


class Graph:
    """A thin, honest wrapper. Nothing here hides a failure."""

    def __init__(self, driver=None, database: Optional[str] = None):
        self._driver = driver
        self.database = database or config.NEO4J.get("database", "neo4j")

    @property
    def driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase
            pw = password()
            if pw is None:
                raise RuntimeError("NEO4J_PASSWORD not in the environment or "
                                   "config.ENV_FILE")
            self._driver = GraphDatabase.driver(config.NEO4J["uri"],
                                                auth=(config.NEO4J["user"], pw))
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    @classmethod
    def available(cls) -> bool:
        try:
            g = cls()
            g.run("RETURN 1 AS ok")
            g.close()
            return True
        except Exception:                                 # noqa: BLE001
            return False

    def run(self, query: str, **params) -> list[dict]:
        with self.driver.session(database=self.database) as session:
            return session.execute_write(
                lambda tx: [r.data() for r in tx.run(query, **params)])

    def read(self, query: str, **params) -> list[dict]:
        with self.driver.session(database=self.database) as session:
            return session.execute_read(
                lambda tx: [r.data() for r in tx.run(query, **params)])

    # ---------------------------------------------------------------- schema
    def ensure_schema(self) -> dict:
        """Constraints first, then indexes. Idempotent by IF NOT EXISTS."""
        for statement in CONSTRAINTS:
            self.run(statement)
        for statement in INDEXES:
            self.run(statement)
        return {"constraints": len(CONSTRAINTS), "indexes": len(INDEXES)}

    # ------------------------------------------------------------------ load
    def merge_nodes(self, rows: Iterable[NodeRow], *, chunk: int = 500) -> dict:
        """Returns what the database did, never what was submitted to it."""
        buckets: dict[tuple, list[dict]] = {}
        submitted = 0
        for row in rows:
            buckets.setdefault((tuple(row.labels), row.key_field), []).append(
                {"key": row.key_value, "props": row.props})
            submitted += 1
        written = 0
        for (labels, key_field), payload in buckets.items():
            query = node_merge(list(labels), key_field)
            for i in range(0, len(payload), chunk):
                result = self.run(query, rows=payload[i:i + chunk])
                written += result[0]["n"] if result else 0
        return {"submitted": submitted, "written": written}

    def partition_edges(self, edges: list[GraphEdge], known_keys: set[str]
                        ) -> tuple[list[GraphEdge], list[dict]]:
        """Split edges into those whose both ends exist, and those deferred.

        An edge whose endpoint MATCH finds nothing writes nothing and used to be
        counted as merged anyway. Rather than discover that afterwards, the ends
        are checked first: `known_keys` are the keys this load is about to
        create, and anything else is looked up in the graph. What is left over
        is deferred, which is a real and expected state during batched
        ingestion, since a ref can resolve to a part that has not arrived.
        """
        wanted: dict[str, set[str]] = {}
        for e in edges:
            bucket = edge_bucket(e)
            for side, value in (("src", e.src), ("dst", e.dst)):
                if value not in known_keys:
                    wanted.setdefault(f"{bucket}|{side}", set()).add(value)
        present: set[str] = set(known_keys)
        by_bucket: dict[str, dict[str, set[str]]] = {}
        for compound, keys in wanted.items():
            bucket, side = compound.split("|", 1)
            by_bucket.setdefault(bucket, {})[side] = keys
        for bucket, sides in by_bucket.items():
            rows = self.read(endpoint_lookup(bucket),
                             src=sorted(sides.get("src", ())),
                             dst=sorted(sides.get("dst", ())))
            present.update(r["key"] for r in rows)

        writable, deferred = [], []
        for e in edges:
            missing = [side for side, value in (("src", e.src), ("dst", e.dst))
                       if value not in present]
            if missing:
                deferred.append({"type": e.type, "src": e.src, "dst": e.dst,
                                 "missing": missing,
                                 "reason": "the far endpoint is not in the graph yet; "
                                           "this edge is deferred until the batch that "
                                           "carries it arrives"})
            else:
                writable.append(e)
        return writable, deferred

    def merge_edges(self, edges: Iterable[GraphEdge], *, load_id: str = "",
                    chunk: int = 500) -> dict:
        buckets: dict[str, list[dict]] = {}
        submitted = 0
        for e in edges:
            row = {"src": e.src, "dst": e.dst, "props": _storable(e.props),
                   "batch_id": e.batch_id, "load_id": load_id}
            if e.type == "USES_TERM":
                row["char_span"] = list(e.props.get("char_span") or [])
            buckets.setdefault(edge_bucket(e), []).append(row)
            submitted += 1
        written = 0
        for bucket, payload in buckets.items():
            query = edge_merge(bucket, discriminated=(bucket == "USES_TERM"))
            for i in range(0, len(payload), chunk):
                result = self.run(query, rows=payload[i:i + chunk])
                written += result[0]["n"] if result else 0
        return {"submitted": submitted, "written": written}

    # ------------------------------------------------------- the three duties
    def rollback(self, batch_id: str, *, limit: int = 10000) -> dict:
        """Remove a batch completely: its relationships, then its nodes.

        Everything the batch asserted goes, except a referent another batch is
        still pointing at. Deleting one of those would take the other batch's
        edge with it and silently unresolve a ref that had a target, which is a
        worse outcome than leaving a Term or a statute standing.
        """
        rels = nodes = 0
        while True:
            n = self.run(Q_ROLLBACK_RELS, batch=batch_id, limit=limit)
            got = n[0]["n"] if n else 0
            rels += got
            if got < limit:
                break
        while True:
            n = self.run(Q_ROLLBACK_NODES, batch=batch_id, limit=limit)
            got = n[0]["n"] if n else 0
            nodes += got
            if got < limit:
                break
        kept = self.read(Q_ROLLBACK_KEPT_REFERENTS, batch=batch_id)
        left = self.read(Q_COUNT_NODES, batch=batch_id)
        return {"op": "rollback", "batch_id": batch_id, "relationships_deleted": rels,
                "nodes_deleted": nodes, "nodes_remaining": left[0]["n"] if left else 0,
                "referents_kept": [k["key"] for k in kept],
                "referents_kept_detail": kept[:50],
                "referents_kept_note": ("shared referents another batch still points "
                                        "at; deleting them would have taken that "
                                        "batch's edges with them")}

    def sweep(self, scope: list[str], batch_id: str, *, load_id: str,
              referent_keys: Optional[list[str]] = None) -> dict:
        """Delete anything in scope this load did not assert.

        Two things bound the blast radius, and both are arguments rather than
        conventions. `scope` is the set of path prefixes this run actually
        loaded, so a sweep after Core Terms can never touch a schedule that came
        in another batch. `load_id` is a content hash of what this load
        asserted, so a rerun of the same batch converges too, which keying on
        batch identity alone could not do.

        Orphan referent cleanup is scoped to the referents this load itself
        asserted. A Concept loaded for one batch whose member provisions arrive
        in a later one is legitimately edgeless in between, and an unrelated
        sweep must not take it.
        """
        doomed = self.read(Q_SWEEP_NODE_IDS, load_id=load_id, scope=scope)
        rels = self.run(Q_SWEEP_RELS, load_id=load_id, scope=scope)
        nodes = self.run(Q_SWEEP_NODES, load_id=load_id, scope=scope)
        orphans: list[dict] = []
        removed_orphans = 0
        keys = sorted(referent_keys or ())
        if keys:
            orphans = self.read(Q_ORPHAN_REFERENTS, keys=keys, load_id=load_id)
            if orphans:
                out = self.run(Q_DELETE_ORPHAN_REFERENTS, keys=keys, load_id=load_id)
                removed_orphans = out[0]["n"] if out else 0
        return {"op": "sweep", "batch_id": batch_id, "load_id": load_id, "scope": scope,
                "relationships_deleted": rels[0]["n"] if rels else 0,
                "nodes_deleted": nodes[0]["n"] if nodes else 0,
                "orphan_referents_deleted": removed_orphans,
                "orphan_referents_considered": len(keys),
                "affected": [d["path"] for d in doomed][:200],
                "affected_ids": [d["id"] for d in doomed][:200],
                "orphan_referents": [o["key"] for o in orphans][:200]}

    def recompute_salience(self, cfg: dict) -> dict:
        """Recompute from the graph, covering every batch it holds.

        DESIGN 3: "because it is a pure function of the graph it recomputes for
        free on every load". Computing it from one batch's stage outputs and
        writing it globally would mean a Core Terms clause never gained breadth
        when the schedule that cites it arrived, which is precisely the thing
        the incremental ingestion demonstrates.
        """
        import math

        furniture_rows = self.read(Q_FURNITURE,
                                   min_repeats=int(cfg["furniture_min_repeats"]),
                                   min_parts=int(cfg["furniture_min_parts"]))
        furniture = sorted({r["id"] for r in furniture_rows})
        self.run(Q_ZERO_SALIENCE)
        self.run(Q_ZERO_TERM_SALIENCE)
        nodes = self.read(Q_NODE_CITATIONS, furniture=furniture)
        terms = self.read(Q_TERM_USES, furniture=furniture)

        flagged = _outliers({r["id"]: r["frequency"] for r in nodes},
                            {r["id"]: r["kind"] for r in nodes},
                            float(cfg["outlier_sigma"]))
        flagged.update(_outliers({r["name"]: r["frequency"] for r in terms},
                                 {r["name"]: "term" for r in terms},
                                 float(cfg["outlier_sigma"])))
        node_rows = [{"id": r["id"],
                      "salience": round(r["breadth"] * math.log(1 + r["frequency"]), 6),
                      "flagged": r["id"] in flagged, "reason": flagged.get(r["id"])}
                     for r in nodes]
        term_rows = [{"name": r["name"],
                      "salience": round(r["breadth"] * math.log(1 + r["frequency"]), 6),
                      "flagged": r["name"] in flagged, "reason": flagged.get(r["name"])}
                     for r in terms]
        written_nodes = written_terms = 0
        for i in range(0, len(node_rows), 500):
            out = self.run(Q_SET_NODE_SALIENCE, rows=node_rows[i:i + 500])
            written_nodes += out[0]["n"] if out else 0
        for i in range(0, len(term_rows), 500):
            out = self.run(Q_SET_TERM_SALIENCE, rows=term_rows[i:i + 500])
            written_terms += out[0]["n"] if out else 0
        top = sorted(node_rows, key=lambda r: (-r["salience"], r["id"]))[:10]
        return {"op": "salience", "source": "graph",
                "note": "recomputed from the whole graph, so every batch loaded so "
                        "far contributes breadth",
                "settings": cfg, "furniture_nodes_excluded": len(furniture),
                "nodes_with_salience": written_nodes, "terms_with_salience": written_terms,
                "flagged": len(flagged),
                "top_nodes": [{"id": r["id"], "salience": r["salience"]} for r in top]}

    def apply_salience(self, node_values: dict[str, float], term_values: dict[str, float],
                       flagged: dict[str, str]) -> dict:
        node_rows = [{"id": key, "salience": value,
                      "flagged": key in flagged, "reason": flagged.get(key)}
                     for key, value in sorted(node_values.items())]
        term_rows = [{"name": key, "salience": value,
                      "flagged": key in flagged, "reason": flagged.get(key)}
                     for key, value in sorted(term_values.items())]
        written_nodes = written_terms = 0
        for i in range(0, len(node_rows), 500):
            out = self.run(Q_SET_NODE_SALIENCE, rows=node_rows[i:i + 500])
            written_nodes += out[0]["n"] if out else 0
        for i in range(0, len(term_rows), 500):
            out = self.run(Q_SET_TERM_SALIENCE, rows=term_rows[i:i + 500])
            written_terms += out[0]["n"] if out else 0
        return {"op": "salience", "nodes_updated": written_nodes,
                "terms_updated": written_terms, "flagged": len(flagged)}

    # ----------------------------------------------------------------- counts
    def counts(self, batch_id: Optional[str] = None) -> dict:
        out = {"nodes_total": self.read(Q_ALL_NODES)[0]["n"],
               "relationships_total": self.read(Q_ALL_RELS)[0]["n"]}
        if batch_id:
            out["nodes_in_batch"] = self.read(Q_COUNT_NODES, batch=batch_id)[0]["n"]
            out["relationships_in_batch"] = self.read(Q_COUNT_RELS, batch=batch_id)[0]["n"]
        by_label = self.read(
            "MATCH (n) UNWIND labels(n) AS l RETURN l AS label, count(*) AS n "
            "ORDER BY label")
        by_type = self.read(
            "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n ORDER BY type")
        out["nodes_by_label"] = {r["label"]: r["n"] for r in by_label}
        out["relationships_by_type"] = {r["type"]: r["n"] for r in by_type}
        return out


def _storable(props: dict) -> dict:
    """Neo4j takes primitives and lists of primitives, nothing else."""
    import json as _json
    out = {}
    for key, value in props.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif isinstance(value, (list, tuple)) and all(
                isinstance(v, (str, int, float, bool)) for v in value):
            out[key] = list(value)
        else:
            out[key] = _json.dumps(value, ensure_ascii=False, default=str)
    return out


def duplicate_edge_keys(edges: list[GraphEdge]) -> list[tuple]:
    seen: set[tuple] = set()
    dupes = []
    for e in edges:
        key = merge_key(e)
        if key in seen:
            dupes.append(key)
        seen.add(key)
    return dupes


def _outliers(frequency: dict[str, int], kind_of: dict[str, str],
              sigma: float) -> dict[str, str]:
    """Frequencies far out of distribution for their kind, flagged not boosted."""
    import math

    by_kind: dict[str, list[int]] = {}
    for key, count in frequency.items():
        by_kind.setdefault(kind_of.get(key, "?"), []).append(count)
    flagged: dict[str, str] = {}
    for kind, counts in by_kind.items():
        if len(counts) < 3:
            continue
        mean = sum(counts) / len(counts)
        stdev = math.sqrt(sum((c - mean) ** 2 for c in counts) / len(counts))
        if stdev == 0:
            continue
        limit = mean + sigma * stdev
        for key, count in frequency.items():
            if kind_of.get(key, "?") == kind and count > limit:
                flagged[key] = (f"frequency {count} is more than {sigma} standard "
                                f"deviations above the mean {mean:.2f} for kind {kind}")
    return flagged
