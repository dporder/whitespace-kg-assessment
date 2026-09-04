"""Stage 5, concepts. `python -m pipeline.concepts`.

Reads stage 2 trees, writes `output/<run>/concepts.json` and side files under
`output/<run>/concepts/`.

    scan.py      the per-unit model call and its structured response
    resolve.py   term collisions, then near-duplicate merging by embedding cosine

Concepts are navigation, never citation. Every record carries `llm_derived:
true` and a confidence the model stated in the same response as the concept
itself. `ASSOCIATED_TERM` is not computed here: it joins stage 4 and stage 5
output, so SPEC 2.4 puts it in stage 7.
"""
from __future__ import annotations

__all__ = ["resolve", "scan"]
