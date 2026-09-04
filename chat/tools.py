"""The seven tools, their schemas, and the ledger that keeps citations honest.

SPEC 6 fixes the list to exactly these seven and makes them the only data
access. Two things happen here that the backends deliberately do not do:

1. `cite` returns PNG bytes from the backend. The model never receives image
   bytes; it receives the page, the box and a URL, and the browser fetches the
   image. Bytes in the transcript would cost a fortune and prove nothing.
2. Every path-and-page pair a tool hands back is recorded in a CitationLedger.
   An answer's citations are checked against it afterwards, so a citation the
   model composed rather than read is detectable rather than merely discouraged.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from . import config as ui_config
from .backends import get_backend
from .backends.base import TOOL_NAMES, ToolBackend

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "find_provision",
        "description": (
            "Find provisions by fuzzy match over paths, numbers, titles and defined terms. "
            "Use it to turn a phrase from the question into concrete paths. Returns hits with "
            "a path and a page; it does not return provision text, so follow up with "
            "get_provision before quoting anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words, a clause number, or a defined term."},
                "limit": {"type": "integer", "description": "Maximum hits, default 8."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_provision",
        "description": (
            "The full text of one provision, derived by walking its children in reading order, "
            "plus its children, its page and its boxes. This is the only source of quotable text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "e.g. core-terms/9/9.2"}},
            "required": ["path"],
        },
    },
    {
        "name": "follow_references",
        "description": (
            "Cross references into or out of a provision. 'outbound' returns the citations made "
            "by the provision and everything under it, with their resolution status; 'inbound' "
            "returns the provisions that cite this one. An ambiguous or unresolved ref is a fact "
            "about the corpus: report it, do not pick a candidate yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "direction": {"type": "string", "enum": ["outbound", "inbound"]},
            },
            "required": ["path", "direction"],
        },
    },
    {
        "name": "define",
        "description": (
            "The definition of a capitalised term: its text, where it is defined, and which "
            "definition site governs in each part. Part-local definitions shadow document-level "
            "ones inside their part, so always check `governs` for the part you are answering about. "
            "Accepts an alias such as CBO."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
        },
    },
    {
        "name": "find_by_concept",
        "description": (
            "Narrow to a neighbourhood by model-derived concept label. Concepts are navigation "
            "only and are never citable: cite the member provisions this returns, never the concept."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        },
    },
    {
        "name": "history",
        "description": (
            "The version chain of a provision, keyed by lineage_key. Only one document version is "
            "loaded, so this reports the current instance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"lineage_key": {"type": "string"}},
            "required": ["lineage_key"],
        },
    },
    {
        "name": "cite",
        "description": (
            "The page-image crop for a provision, rendered from its stored box. Returns the page, "
            "the box and a URL the interface renders; call it for the provisions your answer rests on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]

assert tuple(t["name"] for t in TOOL_SCHEMAS) == TOOL_NAMES, "tool list drifted from SPEC 6"


def crop_url(path: str) -> str:
    return f"/api/crop?path={quote(path, safe='')}"


@dataclass
class CitationLedger:
    """Every (path, page) a tool actually returned."""

    pairs: set[tuple[str, int]] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)

    def record(self, path: str | None, page: Any) -> None:
        if not path:
            return
        self.paths.add(path)
        if isinstance(page, int):
            self.pairs.add((path, page))

    def harvest(self, tool: str, result: dict) -> None:
        if tool == "find_provision":
            for h in result.get("hits", []):
                self.record(h.get("path"), h.get("page"))
        elif tool == "get_provision":
            if result.get("found"):
                self.record(result.get("path"), (result.get("page") or {}).get("start"))
                for b in result.get("boxes", []):
                    self.record(result.get("path"), b.get("page"))
        elif tool == "follow_references":
            for r in result.get("references", []):
                self.record(r.get("ref_path"), r.get("page"))
                self.record(r.get("from_path"), r.get("page"))
        elif tool == "define":
            for s in result.get("sites", []):
                self.record(s.get("definition_path"), s.get("page"))
        elif tool == "find_by_concept":
            for c in result.get("concepts", []):
                for m in c.get("members", []):
                    self.record(m.get("path"), m.get("page"))
        elif tool == "cite":
            if result.get("found"):
                self.record(result.get("path"), result.get("page"))

    def check(self, path: str, page: int | None) -> str:
        """'ok', 'page_mismatch' or 'unknown_path'."""
        if path not in self.paths:
            return "unknown_path"
        if page is None or (path, page) in self.pairs:
            return "ok"
        return "page_mismatch"


@dataclass
class ToolCall:
    name: str
    args: dict
    ok: bool
    summary: str
    ms: int
    result: dict = field(default_factory=dict)
    error: str | None = None


class ToolRunner:
    """Dispatches tool calls, bounds them, and records what came back."""

    def __init__(self, backend: ToolBackend | None = None):
        self.backend = backend or get_backend()
        self.ledger = CitationLedger()
        self.calls: list[ToolCall] = []

    @property
    def exhausted(self) -> bool:
        return len(self.calls) >= ui_config.MAX_TOOL_CALLS

    def run(self, name: str, args: dict) -> ToolCall:
        started = time.perf_counter()
        if name not in TOOL_NAMES:
            call = ToolCall(name, args, False, f"no such tool {name}", 0,
                            error=f"unknown tool {name!r}; the tools are {', '.join(TOOL_NAMES)}")
            self.calls.append(call)
            return call
        try:
            result = self._dispatch(name, args)
            ok, err = True, None
        except Exception as exc:                      # a tool failure is data, not a crash
            result, ok, err = {"error": str(exc)}, False, f"{type(exc).__name__}: {exc}"
        ms = int((time.perf_counter() - started) * 1000)
        if ok:
            self.ledger.harvest(name, result)
        call = ToolCall(name, args, ok, _summarise(name, result, ok), ms, result=result, error=err)
        self.calls.append(call)
        return call

    def _dispatch(self, name: str, args: dict) -> dict:
        b = self.backend
        if name == "find_provision":
            return b.find_provision(str(args["query"]), int(args.get("limit", 8)))
        if name == "get_provision":
            return b.get_provision(str(args["path"]))
        if name == "follow_references":
            return b.follow_references(str(args["path"]), args.get("direction", "outbound"))
        if name == "define":
            return b.define(str(args["term"]))
        if name == "find_by_concept":
            return b.find_by_concept(str(args["label"]))
        if name == "history":
            return b.history(str(args["lineage_key"]))
        if name == "cite":
            out = b.cite(str(args["path"]))
            png = out.pop("png", None)                # bytes never enter the transcript
            if png is not None:
                out["byte_length"] = len(png)
                out["crop_url"] = crop_url(out["path"])
            return out
        raise AssertionError(f"unreachable tool {name}")

    def result_json(self, call: ToolCall) -> str:
        if not call.ok:
            return json.dumps({"error": call.error})
        return json.dumps(call.result, ensure_ascii=False, default=str)


def _summarise(name: str, result: dict, ok: bool) -> str:
    if not ok:
        return "failed"
    if name == "find_provision":
        hits = result.get("hits", [])
        arm = result.get("vector_arm", {})
        extra = "" if arm.get("enabled") else f" · vector arm: {arm.get('status')}"
        return f"{len(hits)} hit{'s' if len(hits) != 1 else ''}{extra}"
    if name == "get_provision":
        if not result.get("found"):
            return "not found"
        pg = (result.get("page") or {}).get("start")
        return f"{result.get('kind')} · page {pg} · {len(result.get('children', []))} children"
    if name == "follow_references":
        refs = result.get("references", [])
        by = {}
        for r in refs:
            by[r.get("status")] = by.get(r.get("status"), 0) + 1
        parts = ", ".join(f"{v} {k}" for k, v in sorted(by.items()))
        return f"{len(refs)} {result.get('direction')}" + (f" · {parts}" if parts else "")
    if name == "define":
        if not result.get("found"):
            return "not defined"
        return f"{len(result.get('sites', []))} site(s) · governs {len(result.get('governs', {}))} part(s)"
    if name == "find_by_concept":
        return f"{len(result.get('concepts', []))} concept(s) · not citable"
    if name == "history":
        return f"{result.get('count', 0)} version(s)"
    if name == "cite":
        if not result.get("found"):
            return result.get("reason", "no crop")
        return f"page {result.get('page')} · {result.get('byte_length', 0)} bytes"
    return "ok"
