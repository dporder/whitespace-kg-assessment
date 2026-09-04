"""Backend selection. One config value decides, the contract does not change."""
from __future__ import annotations

from .. import config as ui_config
from .base import TOOL_NAMES, ToolBackend
from .fixtures import FixturesBackend

_BACKEND: ToolBackend | None = None


def get_backend(force: str | None = None) -> ToolBackend:
    """Resolve chat.config.GRAPH_BACKEND to a live backend.

    "fixtures" the JSON stage outputs (default tonight)
    "neo4j"    read-only parameterised Cypher; raises if unreachable
    "auto"     Neo4j when the graph exists, otherwise the JSON files
    """
    global _BACKEND
    choice = force or ui_config.GRAPH_BACKEND
    if _BACKEND is not None and force is None and _BACKEND.name == _expected(choice):
        return _BACKEND

    if choice == "fixtures":
        backend: ToolBackend = FixturesBackend()
    elif choice == "neo4j":
        from .neo4j_backend import Neo4jBackend

        backend = Neo4jBackend()
    elif choice == "auto":
        from .neo4j_backend import Neo4jBackend

        backend = Neo4jBackend() if Neo4jBackend.available() else FixturesBackend()
    else:
        raise ValueError(
            f"GRAPH_BACKEND must be 'fixtures', 'neo4j' or 'auto', got {choice!r}"
        )
    if force is None:
        _BACKEND = backend
    return backend


def _expected(choice: str) -> str:
    return {"fixtures": "fixtures", "neo4j": "neo4j"}.get(choice, "")


def reset() -> None:
    global _BACKEND
    _BACKEND = None


__all__ = ["get_backend", "reset", "ToolBackend", "FixturesBackend", "TOOL_NAMES"]
