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

Every query here is a module-level constant with $parameters. Relationship types
and labels cannot be parameterised in Cypher, so the few places they appear are
interpolated from this module's own constants, never from input: `_TYPE` and
`_LABEL` reject anything that is not one of the names SPEC 2.5 lists.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Optional

import config
from pipeline.schemas import EdgeType, GraphEdge

from .rows import KIND_LABELS, NodeRow, REFERENT_LABELS, merge_key

EDGE_TYPES = ("CONTAINS", "NEXT", "RESOLVES_TO", "CANDIDATE", "USES_TERM",
              "DEFINED_IN", "ABOUT", "DEFINED_USING", "CONCEPT_REL",
              "ASSOCIATED_TERM", "SUPERSEDES")
ALLOWED_LABELS = {"Node", *KIND_LABELS.values(), *REFERENT_LABELS}
KEY_FIELDS = {"id", "name", "key"}
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
Q_ROLLBACK_NODES = """
MATCH (n) WHERE n.batch_id = $batch
WITH n LIMIT $limit
DETACH DELETE n
RETURN count(*) AS n
"""

# sweep: inside this batch's own scope, anything still wearing an older tag was
# not re-asserted by this run, so state converges instead of only not duplicating
Q_SWEEP_NODE_IDS = """
MATCH (n:Node)
WHERE n.batch_id <> $batch
  AND any(prefix IN $scope WHERE n.path = prefix OR n.path STARTS WITH prefix + '/')
RETURN n.id AS id, n.path AS path, n.batch_id AS batch_id
"""
Q_SWEEP_NODES = """
MATCH (n:Node)
WHERE n.batch_id <> $batch
  AND any(prefix IN $scope WHERE n.path = prefix OR n.path STARTS WITH prefix + '/')
DETACH DELETE n
RETURN count(*) AS n
"""
Q_SWEEP_RELS = """
MATCH (s:Node)-[r]->()
WHERE r.batch_id <> $batch
  AND any(prefix IN $scope WHERE s.path = prefix OR s.path STARTS WITH prefix + '/')
DELETE r
RETURN count(*) AS n
"""
Q_ORPHAN_REFERENTS = """
MATCH (n)
WHERE (n:Term OR n:Legislation OR n:Concept) AND NOT (n)--()
RETURN labels(n) AS labels, coalesce(n.name, n.key, n.id) AS key, n.batch_id AS batch_id
"""
Q_DELETE_ORPHAN_REFERENTS = """
MATCH (n)
WHERE (n:Term OR n:Legislation OR n:Concept) AND NOT (n)--()
DELETE n
RETURN count(*) AS n
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
    setter = f"SET n{extra}, n += row.props" if extra else "SET n += row.props"
    return (f"UNWIND $rows AS row\n"
            f"MERGE (n:{head} {{{field}: row.key}})\n"
            f"{setter}\n"
            f"RETURN count(*) AS n")


def edge_merge(type_: str, discriminated: bool) -> str:
    """MERGE with an explicit key: a rerun updates, it never grows a twin."""
    rel = _type(type_)
    key = (" {char_span: row.char_span}" if discriminated else "")
    return (f"UNWIND $rows AS row\n"
            f"MATCH (a) WHERE a.id = row.src OR a.name = row.src OR a.key = row.src\n"
            f"MATCH (b) WHERE b.id = row.dst OR b.name = row.dst OR b.key = row.dst\n"
            f"MERGE (a)-[r:{rel}{key}]->(b)\n"
            f"SET r += row.props, r.batch_id = row.batch_id\n"
            f"RETURN count(*) AS n")


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
    def merge_nodes(self, rows: Iterable[NodeRow], *, chunk: int = 500) -> int:
        buckets: dict[tuple, list[dict]] = {}
        for row in rows:
            buckets.setdefault((tuple(row.labels), row.key_field), []).append(
                {"key": row.key_value, "props": row.props})
        total = 0
        for (labels, key_field), payload in buckets.items():
            query = node_merge(list(labels), key_field)
            for i in range(0, len(payload), chunk):
                self.run(query, rows=payload[i:i + chunk])
                total += len(payload[i:i + chunk])
        return total

    def merge_edges(self, edges: Iterable[GraphEdge], *, chunk: int = 500) -> int:
        buckets: dict[str, list[dict]] = {}
        for e in edges:
            row = {"src": e.src, "dst": e.dst, "props": _storable(e.props),
                   "batch_id": e.batch_id}
            if e.type == "USES_TERM":
                row["char_span"] = list(e.props.get("char_span") or [])
            buckets.setdefault(e.type, []).append(row)
        total = 0
        for type_, payload in buckets.items():
            query = edge_merge(type_, discriminated=(type_ == "USES_TERM"))
            for i in range(0, len(payload), chunk):
                self.run(query, rows=payload[i:i + chunk])
                total += len(payload[i:i + chunk])
        return total

    # ------------------------------------------------------- the three duties
    def rollback(self, batch_id: str, *, limit: int = 10000) -> dict:
        """Remove a batch completely: its relationships, then its nodes."""
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
        left = self.read(Q_COUNT_NODES, batch=batch_id)
        return {"op": "rollback", "batch_id": batch_id, "relationships_deleted": rels,
                "nodes_deleted": nodes, "nodes_remaining": left[0]["n"] if left else 0}

    def sweep(self, scope: list[str], batch_id: str, *,
              drop_orphan_referents: bool = True) -> dict:
        """Delete anything in scope this batch did not re-assert.

        Scope is what makes this safe: it is the set of path prefixes this run
        actually loaded, so a sweep after loading Core Terms can never touch a
        schedule that arrived in another batch.
        """
        doomed = self.read(Q_SWEEP_NODE_IDS, batch=batch_id, scope=scope)
        rels = self.run(Q_SWEEP_RELS, batch=batch_id, scope=scope)
        nodes = self.run(Q_SWEEP_NODES, batch=batch_id, scope=scope)
        orphans: list[dict] = []
        removed_orphans = 0
        if drop_orphan_referents:
            orphans = self.read(Q_ORPHAN_REFERENTS)
            if orphans:
                out = self.run(Q_DELETE_ORPHAN_REFERENTS)
                removed_orphans = out[0]["n"] if out else 0
        return {"op": "sweep", "batch_id": batch_id, "scope": scope,
                "relationships_deleted": rels[0]["n"] if rels else 0,
                "nodes_deleted": nodes[0]["n"] if nodes else 0,
                "orphan_referents_deleted": removed_orphans,
                "affected": [d["path"] for d in doomed][:200],
                "affected_ids": [d["id"] for d in doomed][:200],
                "orphan_referents": [o["key"] for o in orphans][:200]}

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
