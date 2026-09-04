"""Stage 6, embeddings and summaries. `python -m pipeline.embeddings`.

Reads stage 2 trees, writes `output/<run>/embeddings/`.

    tokens.py      the deterministic token estimate the subtree budget uses
    plan.py        which node is embedded at which altitude, a pure function
    summaries.py   generated summaries for the altitudes raw text cannot serve
    client.py      text-embedding-3-large, batched, content-addressed cache

Vectors are written to a store keyed by content hash and referenced from an
index keyed by node id. They never live on graph nodes, so re-embedding on a new
model is a re-embed rather than a migration.
"""
from __future__ import annotations

__all__ = ["client", "plan", "summaries", "tokens"]
