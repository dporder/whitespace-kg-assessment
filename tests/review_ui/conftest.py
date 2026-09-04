"""Path bootstrap. `review-ui` has a hyphen, so it goes on sys.path directly
and its modules carry a review_ prefix rather than being a package.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "review-ui"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
