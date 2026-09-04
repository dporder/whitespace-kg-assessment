"""The engine-neutral copy of the same graph.

DESIGN 5: "A JSON export of the same graph ships alongside, so nothing about
the design depends on the engine, and the same loader contract would target
ArangoDB or Neptune." SPEC 2.5 puts it in this module deliberately: "The
NetworkX JSON export is written by the same module so graph content has one
producer."

So the export is built from the same `Rows` the Neo4j load consumes, never from
a second traversal of the stage outputs. If the two ever disagreed, one of them
would be lying about what is in the graph.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .rows import Rows


def to_networkx(rows: Rows, meta: Optional[dict] = None):
    """A MultiDiGraph: several edge types legally join the same pair.

    `meta` is stamped on the graph itself, because these files hold the rows of
    one load, not the whole graph. A reader who cannot see which batch and which
    parts a file covers will eventually mistake a batch for the corpus.
    """
    import networkx as nx

    graph = nx.MultiDiGraph(**(meta or {}))
    for row in rows.nodes:
        graph.add_node(row.key_value, labels=list(row.labels), **row.props)
    for edge in rows.edges:
        graph.add_edge(edge.src, edge.dst, key=edge.type, type=edge.type,
                       batch_id=edge.batch_id, **edge.props)
    return graph


def write(rows: Rows, path: Path, meta: Optional[dict] = None) -> dict:
    """`node_link_data`, the shape every NetworkX version reads back."""
    import networkx as nx

    graph = to_networkx(rows, meta)
    try:
        data = nx.node_link_data(graph, edges="links")
    except TypeError:                       # networkx < 3.4 has no `edges` kwarg
        data = nx.node_link_data(graph)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    return {"path": str(path), "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(), "covers": dict(meta or {})}
