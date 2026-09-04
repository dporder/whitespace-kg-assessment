"""UI-side configuration. Owned by ui-builder.

Two switches, each doing exactly one job:

    DATA_SOURCE   where the stage outputs are read from, fixtures/ or output/
    GRAPH_BACKEND which tool backend serves them, the JSON files or Neo4j

Everything else the UIs need (models, thresholds, the PDF path, the .env
location, Neo4j connection details) comes from the repo-root `config.py`,
which the orchestrator owns and this module never shadows.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                      # so `import config` works
    sys.path.insert(0, str(ROOT))                  # however the app is launched

import config as pipeline_config                   # noqa: E402  repo-root config.py

# --- the one switch that moves both UIs off fixtures ------------------------
# "fixtures" reads the hand-made stage outputs committed under fixtures/.
# "output"   reads real pipeline output under output/<OUTPUT_RUN>/.
# Overridable from the environment so a demo can be pointed at a real run
# without editing code: RM6116_DATA_SOURCE=output RM6116_OUTPUT_RUN=<dir>
DATA_SOURCE = os.environ.get("RM6116_DATA_SOURCE", "fixtures").strip() or "fixtures"

# Which run directory under output/ to read when DATA_SOURCE == "output".
# None means "the newest directory that exists".
OUTPUT_RUN: str | None = os.environ.get("RM6116_OUTPUT_RUN") or None

# --- the one switch that moves the chat tools off the JSON files -----------
# "fixtures" serves every tool from the JSON files loaded by chat/source.py.
# "neo4j"    serves them from read-only parameterised Cypher.
# "auto"     uses Neo4j when it is reachable and carries this document, else
#            falls back to the file backend. Same tool contract either way.
# Also settable as RM6116_GRAPH_BACKEND, so the demo can select the real graph
# without editing code.
GRAPH_BACKEND = os.environ.get("RM6116_GRAPH_BACKEND", "fixtures").strip() or "fixtures"

# --- feature flags ----------------------------------------------------------
# The embedding arm of find_provision. Off tonight: stage 6 has not run, so
# there is no vector index and the tool reports that rather than pretending.
EMBEDDING_SEARCH = False

# Bounded tool loop. The loop stops at whichever bound is hit first.
# Raised from 6 after a live run showed the round bound, not the call budget,
# was always what stopped research: richer plans need more rounds, and a longer
# plan makes that worse. Hitting the bound is now survivable (the loop composes
# a final answer from what it gathered), so this is a quality knob rather than a
# safety one. MAX_TOOL_CALLS is the binding limit at this setting.
MAX_TOOL_ROUNDS = 8
MAX_TOOL_CALLS = 24

# Where the interim LLM client writes its call log when pipeline/llm.py is
# absent. Mirrors the SPEC 0 layout, output/<run>/llm_log/.
LLM_LOG_RUN = "chat"


def is_run_dir(p: Path) -> bool:
    """A run directory is one with a tree/ holding at least one part.

    Not every directory under output/ is a pipeline run: llm_cache, llm_log and
    eval sit there too, and picking the alphabetically-last one selected a cache
    directory with no trees, which silently emptied the corpus and turned every
    crop into a 404. The presence of parsed trees is the only thing that makes a
    directory servable, so that is what the test is.
    """
    return p.is_dir() and any((p / "tree").glob("*.json"))


def data_root() -> Path:
    """Directory holding tree/, and optionally refs/, vocab/ and concepts.json."""
    if DATA_SOURCE == "fixtures":
        return ROOT / "fixtures"
    if DATA_SOURCE != "output":
        raise ValueError(f"DATA_SOURCE must be 'fixtures' or 'output', got {DATA_SOURCE!r}")

    out = pipeline_config.OUTPUT
    if OUTPUT_RUN:                       # an explicit pin always wins
        pinned = out / OUTPUT_RUN
        if not pinned.is_dir():
            raise FileNotFoundError(f"OUTPUT_RUN points at {pinned}, which does not exist")
        return pinned

    runs = sorted((p for p in out.glob("*") if is_run_dir(p)), key=lambda p: p.name)
    if not runs:
        present = sorted(p.name for p in out.glob("*") if p.is_dir())
        raise FileNotFoundError(
            f"DATA_SOURCE is 'output' but no directory under {out} contains parsed "
            f"trees. Directories present: {present or 'none'}. Run stages 1 and 2, "
            "pin one with OUTPUT_RUN, or set DATA_SOURCE back to 'fixtures'."
        )
    return runs[-1]


def llm_log_dir() -> Path:
    return pipeline_config.OUTPUT / LLM_LOG_RUN / "llm_log"
