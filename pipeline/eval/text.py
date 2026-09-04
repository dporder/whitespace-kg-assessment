"""Text comparison helpers. Deterministic, and they never alter stored text.

Normalisation here exists to compare two strings, exactly like the hash-only
normalisation in schemas.py. Nothing written back to the graph or the report
passes through it.
"""
from __future__ import annotations

import re
import unicodedata

try:                                                      # listed dependency
    from rapidfuzz import fuzz as _fuzz

    def _ratio(a: str, b: str) -> float:
        return float(_fuzz.ratio(a, b))
except Exception:                                         # pragma: no cover - fallback
    from difflib import SequenceMatcher

    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100.0

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                         "–": "-", "—": "-"})


def normalise(text: str) -> str:
    """Casefolded, punctuation-normalised, whitespace-collapsed comparison key."""
    t = unicodedata.normalize("NFC", text or "").translate(_QUOTES)
    t = re.sub(r"\s+", " ", t).strip().strip(".:;,").casefold()
    return t


def similarity(a: str, b: str) -> float:
    """0 to 100. Comparison only."""
    return round(_ratio(normalise(a), normalise(b)), 1)


def title_case_runs(text: str) -> list[tuple[int, int, str]]:
    """Runs of capitalised words, as (start, end, surface).

    The candidate set for "capitalised phrases used but never defined". Words
    joined by lower-case connectives inside a run ("Terms of the Contract") are
    not merged: the conservative reading keeps the run short, and the harness
    reports the count rather than pretending it is a term list.
    """
    runs: list[tuple[int, int, str]] = []
    start: int | None = None
    end = 0

    def flush() -> None:
        nonlocal start
        if start is None:
            return
        surface = text[start:end]
        # An unbalanced "(" means the run reached into a parenthetical whose
        # close is past the last capitalised word. Cut at the bracket rather
        # than printing "Framework Schedule 4 (Framework Management".
        if surface.count("(") > surface.count(")"):
            surface = surface[:surface.rindex("(")].rstrip()
        if surface:
            runs.append((start, start + len(surface), surface))
        start = None

    for m in re.finditer(r"[A-Za-z][A-Za-z'\-]*", text or ""):
        if m.group(0)[0].isupper():
            if start is None:
                start = m.start()
            end = m.end()
        else:
            flush()
    flush()
    return runs
