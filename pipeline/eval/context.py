"""What every report section is handed. Assembled by __main__, read-only.

Kept separate from report.py so sections can import it without importing the
renderer, and so the set of things a section is allowed to look at is written
down in one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pipeline.eval.golden import GoldenSet
from pipeline.eval.inputs import Inputs
from pipeline.eval.provided import ProvidedOutline, ProvidedPageMap

# Spec-silent knobs. config.py is orchestrator-owned and stage 8 must not edit
# it, so the defaults the SPEC does not name live here, documented, ready to be
# lifted into config.py if the orchestrator wants them tunable.
OUTLINE_TRIAGE_SAMPLE = 20          # disagreements queued for human triage per run
OUTLINE_TITLE_AGREE = 85.0          # rapidfuzz ratio at or above which titles agree
CONCEPT_DUPLICATE_RATIO = 90.0      # lexical proxy for the cosine duplicate check
CONCEPT_PAIR_SCAN_CAP = 400         # concepts compared all-pairs; beyond this the
CONCEPT_TERM_SCAN_CAP = 400         # scans are capped and the report says so
CYCLE_SCC_CAP = 12                  # largest SCC whose cycles are enumerated
GEOMETRY_EPS = 0.5                  # points of slack on every box comparison
BOX_ROUNDTRIP_AGREE = 0.90          # text similarity for the box round-trip check
LIST_CAP = 25                       # rows of any example listing in the report


@dataclass
class Context:
    run: str
    run_dir: Path
    eval_dir: Path
    full: bool
    scope_mode: str                      # batch:<id> | present | full
    scope_parts: list[str]
    cross_check_scope: str               # in_scope_parts | whole_document
    inputs: Inputs
    page_map: ProvidedPageMap
    outline: ProvidedOutline
    golden: GoldenSet
    previous_snapshot: Optional[Path] = None
    batch: Optional[str] = None
    options: dict[str, Any] = field(default_factory=dict)
