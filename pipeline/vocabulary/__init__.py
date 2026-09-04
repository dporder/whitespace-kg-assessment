"""Stage 4, vocabulary. `python -m pipeline.vocabulary`.

Reads stage 2 trees, writes `output/<run>/vocab/*.json`.

    declared.py    the definitions the document sets out, tables and prose
    discovery.py   the drafting-convention rule, run independently
    sites.py       the join, the source flag, and the per-part vocabulary
    typos.py       the deterministic per-section typo-density signal
    matching.py    case sensitive, longest match, no overlaps, typed ambiguity
    routing.py     one narrow prompt per ambiguity kind
    audit.py       the stratified sample of the confident matches
    llmio.py       the shared seam onto pipeline/llm.py, cache and call log
    treeio.py      tree loading and the derived views stages 4 to 6 share

Nothing in this stage alters source text. Term keys are normalised for keying
only, and the raw printed form travels beside every site.
"""
from __future__ import annotations

__all__ = ["audit", "declared", "discovery", "llmio", "matching", "routing",
           "sites", "text", "treeio", "typos"]
