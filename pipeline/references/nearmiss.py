"""Near-miss statute titles: entity resolution, not string matching (SPEC 2.2).

"Two mentions whose normalised keys differ but whose character overlap or
embedding similarity crosses the thresholds in `config.py` (`European Union
(Withdrawal) Act 2018` against a hypothetical `European Union Act 2018`) are
routed, LLM first, human if still uncertain, before either mints a separate
Legislation node."

Character overlap runs first because it is free and deterministic. The
embedding arm sits behind a config flag and is not run tonight: it needs
OpenAI, which is as blocked as Anthropic in this environment, and a silently
skipped arm would be worse than a declared one. Every threshold is read from
`config.py` when the orchestrator adds the block, with the documented defaults
below until then; the run report names every key it wanted.
"""
from __future__ import annotations

from typing import Optional

import config
from pipeline import llm
from pipeline.schemas import Legislation

from .legislation import slug

NEAR_MISS_DEFAULTS = {
    "char_overlap_min": 0.86,      # rapidfuzz ratio over normalised titles, 0..1
    "llm_confidence_min": 0.75,    # below this the pair still goes to a human
    "embedding_enabled": False,    # the second arm, off tonight
    "embedding_cosine_min": 0.92,
}
PROMPT_VERSION = llm.PROMPT_VERSIONS["legislation_near_miss"]
TASK = "reference_hard"

SYSTEM = ("You compare citations to UK and EU legislation and decide whether two "
          "titles name the same instrument. Different instruments often share most "
          "of their words, so say DIFFERENT unless you are confident.")


def thresholds() -> dict:
    out = dict(NEAR_MISS_DEFAULTS)
    out.update(getattr(config, "LEGISLATION_NEAR_MISS", {}) or {})
    return out


def _norm(record: Legislation) -> str:
    return slug(f"{record.title} {record.year}").replace("-", " ")


def _overlap(a: str, b: str) -> float:
    from rapidfuzz import fuzz
    return round(fuzz.ratio(a, b) / 100.0, 3)


def _prompt(a: Legislation, b: Legislation, overlap: float) -> str:
    return f"""Two legislation citations were normalised to different keys, but their
titles overlap by {overlap} on characters. Decide whether they name the same
instrument.

A: title {a.title!r}, year {a.year}, kind {a.instrument_kind}, key {a.key}
B: title {b.title!r}, year {b.year}, kind {b.instrument_kind}, key {b.key}

Score your confidence before you commit to an answer.

Reply with one JSON object with exactly these keys, in this order:
  "considered": "<what makes them alike, and what makes them different>",
  "confidence": <number between 0 and 1>,
  "answer": "SAME" or "DIFFERENT"
"""


def route(records: list[Legislation], *, no_llm: bool = False) -> dict:
    """Every pair above the character threshold, routed and reported."""
    cfg = thresholds()
    unique: dict[str, Legislation] = {}
    for record in records:
        unique.setdefault(record.key, record)
    keys = sorted(unique)
    pairs: list[dict] = []
    reason: Optional[str] = None
    if no_llm:
        reason = "--no-llm: near-miss pairs were not sent to a model"
    elif not llm.available():
        reason = llm.unavailable_reason() or "llm unavailable"

    for i, key_a in enumerate(keys):
        for key_b in keys[i + 1:]:
            a, b = unique[key_a], unique[key_b]
            if a.provision or b.provision:
                continue          # a provision pointer is not a separate instrument
            overlap = _overlap(_norm(a), _norm(b))
            if overlap < float(cfg["char_overlap_min"]):
                continue
            row = {"a": a.key, "b": b.key, "char_overlap": overlap,
                   "arm": "character_overlap", "verdict": None, "confidence": None,
                   "routed_to": "llm", "note": None}
            if reason:
                row["routed_to"] = "queued"
                row["note"] = reason
            else:
                verdict, confidence, note = _ask(a, b, overlap)
                row.update(verdict=verdict, confidence=confidence, note=note)
                if verdict is None or (confidence is not None
                                       and confidence < float(cfg["llm_confidence_min"])):
                    row["routed_to"] = "human"
            pairs.append(row)

    return {
        "thresholds": cfg,
        "config_keys_requested": ["LEGISLATION_NEAR_MISS.char_overlap_min",
                                  "LEGISLATION_NEAR_MISS.llm_confidence_min",
                                  "LEGISLATION_NEAR_MISS.embedding_enabled",
                                  "LEGISLATION_NEAR_MISS.embedding_cosine_min"],
        "distinct_keys": len(keys),
        "pairs_examined": len(keys) * (len(keys) - 1) // 2,
        "pairs_over_threshold": len(pairs),
        "pairs": pairs,
        "embedding_arm": {
            "enabled": bool(cfg["embedding_enabled"]),
            "ran": False,
            "note": ("behind config.LEGISLATION_NEAR_MISS.embedding_enabled; not run: "
                     "it needs text-embedding-3-large and OPENAI_API_KEY, which is as "
                     "blocked as the Anthropic key in this environment"),
        },
        "reason": reason,
    }


def _ask(a: Legislation, b: Legislation, overlap: float
         ) -> tuple[Optional[str], Optional[float], str]:
    try:
        raw = llm.structured(TASK, _prompt(a, b, overlap), system=SYSTEM,
                             prompt_version=PROMPT_VERSION)
    except llm.LLMUnavailable as exc:
        return None, None, str(exc)
    except llm.LLMResponseError as exc:
        return None, None, f"unparseable response: {exc}"
    if not isinstance(raw, dict):
        return None, None, "response was not a JSON object"
    answer = str(raw.get("answer", "")).strip().upper()
    try:
        confidence = round(float(raw.get("confidence")), 3)
    except (TypeError, ValueError):
        confidence = None
    if answer not in ("SAME", "DIFFERENT"):
        return None, confidence, f"answer {answer!r} was neither SAME nor DIFFERENT"
    return answer, confidence, "checked by the model"
