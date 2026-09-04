"""Tool backend over the JSON stage outputs. Tonight's default.

Reads whatever chat.config.DATA_SOURCE points at, so the same code serves
fixtures/ now and output/<run>/ once the pipeline lands.
"""
from __future__ import annotations

from rapidfuzz import fuzz, process

from .. import config as ui_config
from .. import crops
from ..naming import human_citation, name_for_path
from ..source import Corpus, corpus, parent_path_of_ref, part_of
from .base import VECTOR_PENDING, Direction, ToolBackend


class FixturesBackend(ToolBackend):
    name = "fixtures"

    def __init__(self, c: Corpus | None = None):
        self._corpus = c

    @property
    def c(self) -> Corpus:
        return self._corpus if self._corpus is not None else corpus()

    # ------------------------------------------------------------------ find
    def _searchable(self) -> list[tuple[str, str, str]]:
        """(surface, kind_of_match, path) triples the fuzzy matcher scores."""
        rows: list[tuple[str, str, str]] = []
        for path, n in self.c.by_path.items():
            if n.kind == "ref":
                continue
            rows.append((path, "path", path))
            if n.title:
                rows.append((n.title, "title", path))
            if n.label:
                rows.append((f"{n.unit_label or ''} {n.label}".strip(), "label", path))
            if n.text:
                rows.append((n.text, "text", path))
        for site in self.c.definition_sites:
            node = self.c.by_id.get(site.definition_node_id)
            if node is not None:
                rows.append((site.term, "term", node.path))
                for alias in site.aliases:
                    rows.append((alias, "term", node.path))
        return rows

    def find_provision(self, query: str, limit: int = 8) -> dict:
        rows = self._searchable()
        scored = process.extract(
            query,
            [r[0] for r in rows],
            scorer=fuzz.WRatio,
            limit=max(limit * 6, 40),
        )
        best: dict[str, tuple[float, str]] = {}
        for _surface, score, idx in scored:
            _s, matched_on, path = rows[idx]
            prior = best.get(path)
            if prior is None or score > prior[0]:
                best[path] = (score, matched_on)

        hits = []
        for path, (score, matched_on) in sorted(
            best.items(), key=lambda kv: (-kv[1][0], kv[0])
        )[:limit]:
            n = self.c.by_path[path]
            hits.append(
                {
                    "path": path,
                    "name": human_citation(self.c, n),
                    "kind": n.kind,
                    "label": n.label,
                    "title": n.title,
                    "unit_label": n.unit_label,
                    "page": n.page_start,
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
        n = self.c.node(path)
        if n is None:
            return {"path": path, "found": False}
        return {
            "path": path,
            "found": True,
            # how the agreement itself names this provision, so every surface
            # can label it without reaching past the tools for it
            "name": human_citation(self.c, n),
            "kind": n.kind,
            "label": n.label,
            "title": n.title,
            "unit_label": n.unit_label,
            "citable": n.citable,
            "part": part_of(path),
            "lineage_key": n.lineage_key,
            "text": self.c.derived_text(path),
            "own_text": n.text,
            "children": [
                {"path": ch.path, "kind": ch.kind, "label": ch.label, "title": ch.title}
                for ch in n.children
                if ch.kind != "ref"
            ],
            "page": {"start": n.page_start, "end": n.page_end, "printed": n.printed_page},
            "boxes": [{"page": b.page, "bbox": list(b.bbox)} for b in (n.bboxes_own or n.bboxes_extent)],
            "anomalies": list(n.anomalies),
        }

    # ------------------------------------------------------------------ refs
    def _ref_row(self, ref) -> dict:
        parent = parent_path_of_ref(ref.path)
        return {
            "ref_path": ref.path,
            "text": ref.text,
            "from_name": name_for_path(self.c, parent),
            "target_name": name_for_path(self.c, ref.target_path) if ref.target_path else None,
            "ref_kind": ref.ref_kind,
            "status": ref.status,
            "target_path": ref.target_path,
            "scope_rule": ref.scope_rule,
            "resolver": ref.resolver,
            "confidence": ref.confidence,
            "group_id": ref.group_id,
            "candidates": [
                {"path": c.path, "score": c.score, "reason": c.reason,
                 "name": name_for_path(self.c, c.path)} for c in ref.candidates
            ],
            "char_span": list(ref.char_span) if ref.char_span else None,
            "page": ref.page_start,
            "from_path": parent,
        }

    def follow_references(self, path: str, direction: Direction = "outbound") -> dict:
        if direction not in ("outbound", "inbound"):
            raise ValueError("direction must be 'outbound' or 'inbound'")
        if direction == "outbound":
            # refs anchored anywhere in this node's subtree
            prefix = path + "/"
            found = [
                r
                for parent, refs in self.c.refs_by_parent.items()
                if parent == path or parent.startswith(prefix)
                for r in refs
            ]
        else:
            found = list(self.c.refs_by_target.get(path, []))
        rows = sorted((self._ref_row(r) for r in found), key=lambda r: r["ref_path"])
        return {"path": path, "direction": direction, "count": len(rows), "references": rows}

    # ---------------------------------------------------------------- define
    def define(self, term: str) -> dict:
        matched_via = "term"
        sites = self.c.sites_by_term.get(term, [])
        if not sites:
            for name, ss in self.c.sites_by_term.items():
                if any(a == term for s in ss for a in s.aliases):
                    sites, term, matched_via = ss, name, "alias"
                    break
        if not sites:
            lowered = term.casefold()
            for name, ss in self.c.sites_by_term.items():
                if name.casefold() == lowered:
                    sites, term, matched_via = ss, name, "case_insensitive"
                    break
        if not sites:
            return {"term": term, "found": False, "sites": [], "governs": {}}

        rows = []
        for s in sites:
            node = self.c.by_id.get(s.definition_node_id)
            rows.append(
                {
                    "term": s.term,
                    "scope": s.scope,
                    "source": s.source,
                    "aliases": list(s.aliases),
                    "pointer": s.pointer,
                    "definition_path": node.path if node else None,
                    "definition_name": human_citation(self.c, node) if node else None,
                    "definition_text": node.text if node else None,
                    "page": node.page_start if node else None,
                }
            )
        governs = {}
        for part in sorted(self.c.trees):
            g = self.c.governing_site(term, part)
            if g is not None:
                node = self.c.by_id.get(g.definition_node_id)
                governs[part] = {
                    "scope": g.scope,
                    "definition_path": node.path if node else None,
                }
        return {
            "term": term,
            "found": True,
            "matched_via": matched_via,
            "aliases": sorted({a for s in sites for a in s.aliases}),
            "sites": rows,
            "governs": governs,
            "note": "part-local definitions shadow document-level ones inside their part",
        }

    # --------------------------------------------------------------- concepts
    def find_by_concept(self, label: str) -> dict:
        if not self.c.concepts:
            return {"label": label, "found": False, "concepts": [], "citable": False}
        names = [c.label for c in self.c.concepts]
        scored = process.extract(label, names, scorer=fuzz.WRatio, limit=len(names))
        out = []
        for _s, score, idx in scored:
            if score < 60:
                continue
            con = self.c.concepts[idx]
            members = []
            for nid in con.member_node_ids:
                n = self.c.by_id.get(nid)
                if n is not None:
                    members.append(
                        {"path": n.path, "kind": n.kind, "label": n.label, "page": n.page_start}
                    )
            out.append(
                {
                    "id": con.id,
                    "label": con.label,
                    "scope_path": con.scope_path,
                    "confidence": con.confidence,
                    "llm_derived": con.llm_derived,
                    "score": round(score / 100.0, 3),
                    "members": members,
                    "relations": [
                        {"src": r.src, "label": r.label, "dst": r.dst} for r in con.relations
                    ],
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
        current = [n for n in self.c.by_path.values() if n.lineage_key == lineage_key]
        versions = [
            {
                "node_id": n.id,
                "path": n.path,
                "version_label": None,
                "content_hash": n.content_hash,
                "batch_id": n.batch_id,
                "page": n.page_start,     # so a claim sourced from history is citable
            }
            for n in sorted(current, key=lambda n: n.path)
        ]
        return {
            "lineage_key": lineage_key,
            "count": len(versions),
            "versions": versions,
            "note": (
                "one document version is loaded, so this is the current instance only. "
                "SUPERSEDES chains are schema-only tonight."
            ),
        }

    # ------------------------------------------------------------------- cite
    def cite(self, path: str) -> dict:
        n = self.c.node(path)
        if n is None:
            return {"path": path, "found": False}
        box = self.c.first_box(n)
        if box is None:
            return {"path": path, "found": False, "reason": "node carries no bbox"}
        # A chat citation is always drawn in the deterministic blue: the box
        # marks where the quoted words are, not what the pipeline thinks of
        # them. The review queue colours by tier, where that distinction is
        # the whole point of the row.
        png = crops.render_crop(box["page"], box["bbox"], colour="deterministic")
        return {
            "path": path,
            "found": True,
            "name": human_citation(self.c, n),
            "page": box["page"],
            "bbox": box["bbox"],
            "media_type": "image/png",
            "png": png,
        }
