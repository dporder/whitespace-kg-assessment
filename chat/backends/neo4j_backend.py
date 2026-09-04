"""Tool backend over Neo4j. Activates when the graph exists.

Read-only and parameterised throughout: every query is a module-level constant
with $parameters, run through `session.execute_read` on a READ-access session.
No query is ever assembled by string concatenation or f-string, so no caller
input reaches Cypher as syntax.

The graph model is SPEC 2.5: every node carries :Node plus a kind label, the
referents are :Term (name), :Legislation (key) and :Concept (id), and the tree
is CONTAINS, including provision to ref.

One thing SPEC 2.5 does not pin down: how a node's bboxes are stored, since
Neo4j properties cannot hold a list of maps. `_boxes` accepts the three
encodings a loader could reasonably choose (a JSON string, a list of JSON
strings, or flat parallel arrays) and degrades to page-only citation if it
recognises none. Reconcile with the loader when stage 7 lands.
"""
from __future__ import annotations

import json
import os
from typing import Any

from rapidfuzz import fuzz, process

from .. import config as ui_config
from .. import crops
from ..source import part_of
from .base import VECTOR_PENDING, Direction, ToolBackend

NEO4J = ui_config.pipeline_config.NEO4J

# --- parameterised, read-only Cypher ---------------------------------------

Q_PROBE = "MATCH (n:Node) RETURN count(n) AS n"

Q_INDEX_ROWS = """
MATCH (n:Node)
WHERE n.kind <> 'ref'
RETURN n.path AS path, n.kind AS kind, n.label AS label, n.title AS title,
       n.unit_label AS unit_label, n.page_start AS page_start
"""

Q_TERM_ROWS = """
MATCH (t:Term)-[:DEFINED_IN]->(d:Node)
RETURN t.name AS name, d.path AS path
"""

Q_NODE = """
MATCH (n:Node {path: $path})
RETURN n AS n
"""

Q_DERIVED_TEXT = """
MATCH (n:Node {path: $path})-[:CONTAINS*0..]->(d:Node)
WHERE d.text IS NOT NULL AND d.kind <> 'ref'
RETURN d.text AS text
ORDER BY d.order
"""

Q_CHILDREN = """
MATCH (n:Node {path: $path})-[:CONTAINS]->(c:Node)
WHERE c.kind <> 'ref'
RETURN c.path AS path, c.kind AS kind, c.label AS label, c.title AS title
ORDER BY c.order
"""

Q_REFS_OUT = """
MATCH (n:Node {path: $path})-[:CONTAINS*0..]->(p:Node)-[:CONTAINS]->(r:Node)
WHERE r.kind = 'ref'
OPTIONAL MATCH (r)-[cand:CANDIDATE]->(c:Node)
RETURN r AS r, p.path AS from_path,
       collect({path: c.path, score: cand.score, reason: cand.reason}) AS candidates
"""

Q_REFS_IN = """
MATCH (r:Node)-[:RESOLVES_TO]->(t:Node {path: $path})
WHERE r.kind = 'ref'
MATCH (p:Node)-[:CONTAINS]->(r)
OPTIONAL MATCH (r)-[cand:CANDIDATE]->(c:Node)
RETURN r AS r, p.path AS from_path,
       collect({path: c.path, score: cand.score, reason: cand.reason}) AS candidates
"""

Q_DEFINE = """
MATCH (t:Term {name: $term})-[d:DEFINED_IN]->(n:Node)
RETURN t AS t, d AS d, n.path AS definition_path, n.text AS definition_text,
       n.page_start AS page
"""

Q_TERM_BY_ALIAS = """
MATCH (t:Term)
WHERE $alias IN coalesce(t.aliases, [])
RETURN t.name AS name
LIMIT 1
"""

Q_CONCEPTS = """
MATCH (c:Concept)
OPTIONAL MATCH (p:Node)-[:ABOUT]->(c)
OPTIONAL MATCH (c)-[rel:CONCEPT_REL]->(o:Concept)
RETURN c AS c,
       collect(DISTINCT {path: p.path, kind: p.kind, label: p.label, page: p.page_start}) AS members,
       collect(DISTINCT {src: c.id, label: rel.label, dst: o.id}) AS relations
"""

Q_HISTORY = """
MATCH (n:Node {lineage_key: $lineage_key})
RETURN n.id AS node_id, n.path AS path, n.content_hash AS content_hash,
       n.batch_id AS batch_id, n.version_label AS version_label
ORDER BY coalesce(n.version_label, ''), n.path
"""


def _boxes(props: dict) -> list[dict]:
    """Decode bboxes from whatever encoding the loader chose. See module docstring."""
    for key in ("bboxes_own", "bboxes_extent"):
        raw = props.get(key)
        if not raw:
            continue
        if isinstance(raw, str):                       # one JSON string
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                return [b for b in parsed if isinstance(b, dict) and "bbox" in b]
        if isinstance(raw, list) and raw and isinstance(raw[0], str):   # list of JSON strings
            out = []
            for item in raw:
                try:
                    b = json.loads(item)
                except json.JSONDecodeError:
                    continue
                if isinstance(b, dict) and "bbox" in b:
                    out.append(b)
            if out:
                return out
        if isinstance(raw, list) and len(raw) == 4 and all(
            isinstance(v, (int, float)) for v in raw
        ):                                             # flat single box
            page = props.get("page_start")
            if page is not None:
                return [{"page": page, "bbox": list(raw)}]
    return []


class Neo4jBackend(ToolBackend):
    name = "neo4j"

    def __init__(self, driver=None):
        self._driver = driver
        self._database = NEO4J.get("database", "neo4j")

    # ------------------------------------------------------------- connection
    @staticmethod
    def _password() -> str | None:
        pw = os.environ.get("NEO4J_PASSWORD")
        if pw:
            return pw
        try:
            from dotenv import dotenv_values
        except ImportError:
            return None
        env_file = ui_config.pipeline_config.ENV_FILE
        if env_file.exists():
            return dotenv_values(env_file).get("NEO4J_PASSWORD")
        return None

    @property
    def driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase

            pw = self._password()
            if pw is None:
                raise RuntimeError("NEO4J_PASSWORD not in the environment or config.ENV_FILE")
            self._driver = GraphDatabase.driver(NEO4J["uri"], auth=(NEO4J["user"], pw))
        return self._driver

    def _read(self, query: str, **params) -> list[dict]:
        from neo4j import READ_ACCESS

        with self.driver.session(database=self._database, default_access_mode=READ_ACCESS) as s:
            return s.execute_read(lambda tx: [r.data() for r in tx.run(query, **params)])

    def _read_records(self, query: str, **params) -> list[Any]:
        from neo4j import READ_ACCESS

        with self.driver.session(database=self._database, default_access_mode=READ_ACCESS) as s:
            return s.execute_read(lambda tx: list(tx.run(query, **params)))

    @classmethod
    def available(cls) -> bool:
        """True when the graph is reachable and carries at least one node."""
        try:
            b = cls()
            rows = b._read(Q_PROBE)
            return bool(rows) and rows[0].get("n", 0) > 0
        except Exception:
            return False

    # ------------------------------------------------------------------ find
    def find_provision(self, query: str, limit: int = 8) -> dict:
        # Scored in Python with the same rapidfuzz scorer the file backend
        # uses, so both backends rank alike. At corpus scale this becomes a
        # Neo4j full-text index; the tool contract does not change.
        rows = self._read(Q_INDEX_ROWS)
        terms = self._read(Q_TERM_ROWS)
        surfaces: list[tuple[str, str, str]] = []
        meta: dict[str, dict] = {}
        for r in rows:
            meta[r["path"]] = r
            surfaces.append((r["path"], "path", r["path"]))
            if r.get("title"):
                surfaces.append((r["title"], "title", r["path"]))
            if r.get("label"):
                surfaces.append((f"{r.get('unit_label') or ''} {r['label']}".strip(), "label", r["path"]))
        for t in terms:
            if t.get("path") in meta:
                surfaces.append((t["name"], "term", t["path"]))

        scored = process.extract(
            query, [s[0] for s in surfaces], scorer=fuzz.WRatio, limit=max(limit * 6, 40)
        )
        best: dict[str, tuple[float, str]] = {}
        for _surface, score, idx in scored:
            _s, matched_on, path = surfaces[idx]
            if path not in best or score > best[path][0]:
                best[path] = (score, matched_on)

        hits = []
        for path, (score, matched_on) in sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:limit]:
            r = meta[path]
            hits.append(
                {
                    "path": path,
                    "kind": r.get("kind"),
                    "label": r.get("label"),
                    "title": r.get("title"),
                    "unit_label": r.get("unit_label"),
                    "page": r.get("page_start"),
                    "score": round(score / 100.0, 3),
                    "matched_on": matched_on,
                }
            )
        return {
            "query": query,
            "backend": self.name,
            "hits": hits,
            "vector_arm": {
                "enabled": bool(ui_config.EMBEDDING_SEARCH),
                "status": VECTOR_PENDING if not ui_config.EMBEDDING_SEARCH else "ready",
            },
        }

    # ------------------------------------------------------------------- get
    def get_provision(self, path: str) -> dict:
        recs = self._read_records(Q_NODE, path=path)
        if not recs:
            return {"path": path, "found": False}
        props = dict(recs[0]["n"])
        text = "\n".join(r["text"] for r in self._read(Q_DERIVED_TEXT, path=path) if r.get("text"))
        children = self._read(Q_CHILDREN, path=path)
        return {
            "path": path,
            "found": True,
            "kind": props.get("kind"),
            "label": props.get("label"),
            "title": props.get("title"),
            "unit_label": props.get("unit_label"),
            "citable": props.get("citable"),
            "part": part_of(path),
            "lineage_key": props.get("lineage_key"),
            "text": text,
            "own_text": props.get("text"),
            "children": children,
            "page": {
                "start": props.get("page_start"),
                "end": props.get("page_end"),
                "printed": props.get("printed_page"),
            },
            "boxes": _boxes(props),
            "anomalies": list(props.get("anomalies") or []),
        }

    # ------------------------------------------------------------------ refs
    def follow_references(self, path: str, direction: Direction = "outbound") -> dict:
        if direction not in ("outbound", "inbound"):
            raise ValueError("direction must be 'outbound' or 'inbound'")
        query = Q_REFS_OUT if direction == "outbound" else Q_REFS_IN
        rows = []
        for rec in self._read_records(query, path=path):
            r = dict(rec["r"])
            cands = [c for c in rec["candidates"] if c and c.get("path")]
            span = r.get("char_span")
            rows.append(
                {
                    "ref_path": r.get("path"),
                    "text": r.get("text"),
                    "ref_kind": r.get("ref_kind"),
                    "status": r.get("status"),
                    "target_path": r.get("target_path"),
                    "scope_rule": r.get("scope_rule"),
                    "resolver": r.get("resolver"),
                    "confidence": r.get("confidence"),
                    "group_id": r.get("group_id"),
                    "candidates": cands,
                    "char_span": list(span) if span else None,
                    "page": r.get("page_start"),
                    "from_path": rec["from_path"],
                }
            )
        rows.sort(key=lambda r: r["ref_path"] or "")
        return {"path": path, "direction": direction, "count": len(rows), "references": rows}

    # ---------------------------------------------------------------- define
    def define(self, term: str) -> dict:
        matched_via = "term"
        recs = self._read_records(Q_DEFINE, term=term)
        if not recs:
            alias = self._read(Q_TERM_BY_ALIAS, alias=term)
            if alias:
                term, matched_via = alias[0]["name"], "alias"
                recs = self._read_records(Q_DEFINE, term=term)
        if not recs:
            return {"term": term, "found": False, "sites": [], "governs": {}}

        sites, aliases = [], set()
        for rec in recs:
            t, d = dict(rec["t"]), dict(rec["d"])
            scope = d.get("scope") or t.get("scope") or "document"
            for a in t.get("aliases") or []:
                aliases.add(a)
            sites.append(
                {
                    "term": t.get("name", term),
                    "scope": scope,
                    "source": d.get("source") or t.get("source"),
                    "aliases": list(t.get("aliases") or []),
                    "pointer": d.get("pointer") or t.get("pointer"),
                    "definition_path": rec["definition_path"],
                    "definition_text": rec["definition_text"],
                    "page": rec["page"],
                }
            )

        parts = sorted({part_of(s["definition_path"]) for s in sites if s["definition_path"]})
        governs = {}
        for part in parts:
            local = next((s for s in sites if s["scope"] == f"part:{part}"), None)
            chosen = local or next((s for s in sites if s["scope"] == "document"), None)
            if chosen:
                governs[part] = {
                    "scope": chosen["scope"],
                    "definition_path": chosen["definition_path"],
                }
        return {
            "term": term,
            "found": True,
            "matched_via": matched_via,
            "aliases": sorted(aliases),
            "sites": sites,
            "governs": governs,
            "note": "part-local definitions shadow document-level ones inside their part",
        }

    # --------------------------------------------------------------- concepts
    def find_by_concept(self, label: str) -> dict:
        recs = self._read_records(Q_CONCEPTS)
        if not recs:
            return {"label": label, "found": False, "concepts": [], "citable": False}
        cons = [dict(r["c"]) for r in recs]
        names = [c.get("label", "") for c in cons]
        scored = process.extract(label, names, scorer=fuzz.WRatio, limit=len(names))
        out = []
        for _s, score, idx in scored:
            if score < 60:
                continue
            c, rec = cons[idx], recs[idx]
            out.append(
                {
                    "id": c.get("id"),
                    "label": c.get("label"),
                    "scope_path": c.get("scope_path"),
                    "confidence": c.get("confidence"),
                    "llm_derived": c.get("llm_derived", True),
                    "score": round(score / 100.0, 3),
                    "members": [m for m in rec["members"] if m and m.get("path")],
                    "relations": [r for r in rec["relations"] if r and r.get("dst")],
                }
            )
        return {
            "label": label,
            "found": bool(out),
            "concepts": out,
            "citable": False,
            "note": "concepts narrow the search; quote and cite the member provisions, never the concept",
        }

    # ---------------------------------------------------------------- history
    def history(self, lineage_key: str) -> dict:
        versions = self._read(Q_HISTORY, lineage_key=lineage_key)
        return {
            "lineage_key": lineage_key,
            "count": len(versions),
            "versions": versions,
            "note": "version chain over lineage_key; SUPERSEDES roots are schema-only tonight",
        }

    # ------------------------------------------------------------------- cite
    def cite(self, path: str) -> dict:
        recs = self._read_records(Q_NODE, path=path)
        if not recs:
            return {"path": path, "found": False}
        props = dict(recs[0]["n"])
        boxes = _boxes(props)
        if not boxes:
            return {
                "path": path,
                "found": False,
                "page": props.get("page_start"),
                "reason": "node carries no decodable bbox property",
            }
        box = boxes[0]
        png = crops.render_crop(box["page"], box["bbox"])
        return {
            "path": path,
            "found": True,
            "page": box["page"],
            "bbox": list(box["bbox"]),
            "media_type": "image/png",
            "png": png,
        }
