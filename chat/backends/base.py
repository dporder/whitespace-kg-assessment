"""The tool interface. Two backends implement it, nothing else reaches data.

SPEC 6 fixes the tool list to exactly seven. They are the only data access the
chat agent has: there is no side channel to the trees, the graph or the PDF,
so any claim in an answer can be traced to a call recorded in the transcript.

Both backends return the same shapes, documented per method below and pinned
by tests/chat/test_tool_contracts.py. The switch is chat.config.GRAPH_BACKEND.
"""
from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

Direction = Literal["outbound", "inbound"]

TOOL_NAMES = (
    "find_provision",
    "get_provision",
    "follow_references",
    "define",
    "find_by_concept",
    "history",
    "cite",
)

VECTOR_PENDING = "vector index pending"


@runtime_checkable
class ToolBackend(Protocol):
    """Every method returns a JSON-serialisable dict, except `cite`, which may
    carry PNG bytes under "png" for the transport layer to strip."""

    name: str

    def find_provision(self, query: str, limit: int = 8) -> dict:
        """Fuzzy over paths, titles and terms, plus an embedding arm behind a
        feature flag.

        {"query", "backend", "hits": [{"path", "kind", "label", "title",
         "unit_label", "page", "score", "matched_on"}], "vector_arm":
         {"enabled": bool, "status": str}}
        """

    def get_provision(self, path: str) -> dict:
        """Derived text (children walked in order), children, page and box.

        {"path", "found", "kind", "label", "title", "unit_label", "citable",
         "part", "lineage_key", "text", "own_text", "children": [...],
         "page": {"start", "end", "printed"}, "boxes": [...], "anomalies": [...]}
        """

    def follow_references(self, path: str, direction: Direction = "outbound") -> dict:
        """{"path", "direction", "count", "references": [{"ref_path", "text",
        "ref_kind", "status", "target_path", "scope_rule", "resolver",
        "confidence", "candidates", "char_span", "page", "from_path"}]}"""

    def define(self, term: str) -> dict:
        """Definition text, source, and which site governs in each part.

        {"term", "found", "matched_via", "aliases", "sites": [...],
         "governs": {part: {"scope", "definition_path"}}, "note"}
        """

    def find_by_concept(self, label: str) -> dict:
        """{"label", "found", "concepts": [...], "citable": False, "note"}"""

    def history(self, lineage_key: str) -> dict:
        """{"lineage_key", "count", "versions": [...], "note"}"""

    def cite(self, path: str) -> dict:
        """{"path", "found", "page", "bbox", "media_type", "png": bytes}"""


def not_found(**extra) -> dict:
    d = {"found": False}
    d.update(extra)
    return d
