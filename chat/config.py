"""UI-side configuration. Owned by ui-builder.

Two switches, each doing exactly one job:

    DATA_SOURCE   where the stage outputs are read from, fixtures/ or output/
    GRAPH_BACKEND which tool backend serves them, the JSON files or Neo4j

Everything else the UIs need (models, thresholds, the PDF path, the .env
location, Neo4j connection details) comes from the repo-root `config.py`,
which the orchestrator owns and this module never shadows.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                      # so `import config` works
    sys.path.insert(0, str(ROOT))                  # however the app is launched

import config as pipeline_config                   # noqa: E402  repo-root config.py

# --- the one switch that moves both UIs off fixtures ------------------------
# "fixtures" reads the hand-made stage outputs committed under fixtures/.
# "output"   reads real pipeline output under output/<OUTPUT_RUN>/.
DATA_SOURCE = "fixtures"

# Which run directory under output/ to read when DATA_SOURCE == "output".
# None means "the newest directory that exists".
OUTPUT_RUN: str | None = None

# --- the one switch that moves the chat tools off the JSON files -----------
# "fixtures" serves every tool from the JSON files loaded by chat/source.py.
# "neo4j"    serves them from read-only parameterised Cypher.
# "auto"     uses Neo4j when it is reachable and carries this document, else
#            falls back to the file backend. Same tool contract either way.
GRAPH_BACKEND = "fixtures"

# --- feature flags ----------------------------------------------------------
# The embedding arm of find_provision. Off tonight: stage 6 has not run, so
# there is no vector index and the tool reports that rather than pretending.
EMBEDDING_SEARCH = False

# Bounded tool loop. The loop stops at whichever bound is hit first.
MAX_TOOL_ROUNDS = 6
MAX_TOOL_CALLS = 24

# Where the interim LLM client writes its call log when pipeline/llm.py is
# absent. Mirrors the SPEC 0 layout, output/<run>/llm_log/.
LLM_LOG_RUN = "chat"


def data_root() -> Path:
    """Directory holding tree/, refs/, vocab/ and concepts.json."""
    if DATA_SOURCE == "fixtures":
        return ROOT / "fixtures"
    if DATA_SOURCE != "output":
        raise ValueError(f"DATA_SOURCE must be 'fixtures' or 'output', got {DATA_SOURCE!r}")
    out = pipeline_config.OUTPUT
    if OUTPUT_RUN:
        return out / OUTPUT_RUN
    runs = sorted((p for p in out.glob("*") if p.is_dir()), key=lambda p: p.name)
    if not runs:
        raise FileNotFoundError(
            f"DATA_SOURCE is 'output' but no run directory exists under {out}. "
            "Run the pipeline, or set DATA_SOURCE back to 'fixtures'."
        )
    return runs[-1]


def llm_log_dir() -> Path:
    return pipeline_config.OUTPUT / LLM_LOG_RUN / "llm_log"
