"""Shared test scaffolding. Owned by eval-builder, used by every builder's tests.

Puts the repo root on `sys.path` so `import config` and `from pipeline...`
work when pytest is run from the repo root as `pytest` or `.venv/bin/pytest`,
not only as `python -m pytest`. Every builder's tests under `tests/<stage>/`
inherit this; nothing else here is stage specific.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
