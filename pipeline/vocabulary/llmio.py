"""The enrichment stages' LLM seam: replay cache, call log, honest degradation.

All model calls go through `pipeline/llm.py`, which resolver-builder owns and
which does not exist yet. Its contract is `complete(task: str, prompt: str) ->
str`, with `task` selecting the model from `config.MODELS`. This module is the
thin layer the three enrichment stages sit behind it:

* **it delegates to `pipeline.llm` whenever that module is importable**, which
  is the SPEC rule of one LLM path: that module then owns the call and its own
  replay cache, and this one neither second-guesses nor shadows it;
* when `pipeline/llm.py` is absent it falls back to a local replay cache keyed
  on `sha1(model|task|prompt_version|prompt)` under `output/llm_cache/`, and
  records a **pending** result rather than raising or, worse, inventing content.
  The two caches are keyed differently on purpose: consulting both would let a
  rerun be served by whichever answered first, and the log would stop describing
  what actually ran;
* it appends every call to `output/<run>/llm_log/<stage>.jsonl` with model,
  prompt version and raw response, per SPEC ground rules, and with no clock
  anywhere so two runs over the same input produce the same bytes.

A pending call is never a silent zero. Every caller writes the pending item, its
prompt and the reason into its stage's output, so one rerun completes the work
and a reader can see exactly what did not happen.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config

ENTRY_POINT = "complete"

PENDING_MODULE = "pending_llm_module"
PENDING_CREDENTIALS = "pending_credentials"
REPLAYED = "replayed"
CALLED = "called"
DELEGATED = "delegated"
FAILED = "failed"
DISABLED = "disabled"

# Substrings that mark a provider refusal we cannot fix by retrying tonight.
_CREDENTIAL_MARKERS = ("authentication", "unauthorized", "unauthorised", "api key",
                       "credit", "quota", "billing", "permission", "invalid_api_key",
                       "401", "403", "429")


@dataclass
class Call:
    task: str
    model: Optional[str]
    prompt_version: str
    prompt: str
    key: str
    state: str
    response: Optional[str] = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.state in (REPLAYED, CALLED, DELEGATED) and self.response is not None

    @property
    def pending(self) -> bool:
        return self.state in (PENDING_MODULE, PENDING_CREDENTIALS)

    def as_dict(self, with_prompt: bool = False) -> dict:
        out = {"task": self.task, "model": self.model,
               "prompt_version": self.prompt_version, "cache_key": self.key,
               "state": self.state, "note": self.note,
               "response_chars": len(self.response or "")}
        if with_prompt:
            out["prompt"] = self.prompt
        return out


def cache_key(model: Optional[str], task: str, prompt_version: str, prompt: str) -> str:
    material = f"{model}|{task}|{prompt_version}|{prompt}"
    return hashlib.sha1(material.encode()).hexdigest()


_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(?P<body>.*?)\n?```", re.S)


def strip_fence(raw: str) -> str:
    """Unwrap a markdown code fence around a JSON reply.

    Prompts here ask for "a JSON array and nothing else", and models mostly
    comply, but not always: the first live routing run came back fenced from
    Claude Haiku 4.5 and every verdict was thrown away as unparseable. A fence
    is a formatting habit, not a different answer, so it is unwrapped rather
    than failing the batch. Anything else stays strict, and a reply that is not
    JSON underneath still fails loudly.
    """
    if not raw:
        return raw
    match = _FENCE.search(raw)
    return match.group("body") if match else raw.strip()


def _classify(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    return PENDING_CREDENTIALS if any(m in text for m in _CREDENTIAL_MARKERS) else FAILED


@dataclass
class Runner:
    """One stage's model seam. Construct once per run, ask it for completions."""
    stage: str
    run_dir: Path
    cache_root: Path
    enabled: bool = True
    calls: list[Call] = field(default_factory=list)
    _log_lines: list[str] = field(default_factory=list)

    # -- availability -------------------------------------------------------
    @staticmethod
    def entry_point():
        try:
            from pipeline import llm                       # noqa: PLC0415
        except Exception:                                  # noqa: BLE001
            return None
        fn = getattr(llm, ENTRY_POINT, None)
        return fn if callable(fn) else None

    @staticmethod
    def availability() -> dict:
        fn = Runner.entry_point()
        return {"entry_point": f"pipeline.llm.{ENTRY_POINT}",
                "available": fn is not None,
                "note": ("pipeline.llm.complete(task, prompt) is importable"
                         if fn is not None else
                         "pending llm.py: pipeline/llm.py is not present (or exposes "
                         "no callable complete(task, prompt)), so model-backed steps "
                         "are queued, not run")}

    # -- cache --------------------------------------------------------------
    def _cache_path(self, task: str, key: str) -> Path:
        return self.cache_root / task / f"{key}.json"

    def read_cache(self, task: str, key: str) -> Optional[str]:
        path = self._cache_path(task, key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())["response"]
        except Exception:                                  # noqa: BLE001
            return None

    def write_cache(self, call: Call) -> None:
        path = self._cache_path(call.task, call.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "task": call.task, "model": call.model,
            "prompt_version": call.prompt_version, "prompt": call.prompt,
            "response": call.response}, indent=2, ensure_ascii=False) + "\n")

    # -- the call ------------------------------------------------------------
    @staticmethod
    def _accepts_max_tokens(fn) -> bool:
        """Does this client take a max_tokens budget?

        The pinned contract is `complete(task, prompt) -> str`, so the budget is
        offered rather than assumed: a client without it is called the old way
        instead of dying on an unexpected keyword.
        """
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):                    # noqa: BLE001
            return False
        return "max_tokens" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    def complete(self, task: str, prompt_version: str, prompt: str,
                 max_tokens: Optional[int] = None) -> Call:
        """Delegate to `pipeline.llm` when it is there; fall back when it is not.

        SPEC's rule is one LLM path, so when `pipeline.llm` is importable it owns
        the call *and its replay cache*: this module does not consult its own
        cache first, because two caches keyed differently would mean a rerun
        could be served by whichever happened to answer, and the log would stop
        describing what actually ran. The local cache below is the documented
        fallback for the window in which `pipeline/llm.py` does not yet exist.
        """
        model = config.MODELS.get(task)
        key = cache_key(model, task, prompt_version, prompt)
        if not self.enabled:
            return self._record(Call(task, model, prompt_version, prompt, key,
                                     DISABLED, None, "--no-llm: not called"))
        fn = self.entry_point()
        if fn is not None:
            kwargs = {}
            if max_tokens and self._accepts_max_tokens(fn):
                kwargs["max_tokens"] = int(max_tokens)
            try:
                raw = fn(task, prompt, **kwargs)
            except Exception as exc:                       # noqa: BLE001
                return self._record(Call(task, model, prompt_version, prompt, key,
                                         _classify(exc),
                                         None, f"{type(exc).__name__}: {exc}"[:400]))
            note = "delegated to pipeline.llm, which owns the replay cache"
            if kwargs:
                note += f"; max_tokens={kwargs['max_tokens']}"
            return self._record(Call(task, model, prompt_version, prompt, key,
                                     DELEGATED, raw, note))

        # -- fallback: pipeline/llm.py is absent -----------------------------
        cached = self.read_cache(task, key)
        if cached is not None:
            return self._record(Call(task, model, prompt_version, prompt, key,
                                     REPLAYED, cached,
                                     "served from the local fallback cache; "
                                     "pipeline.llm is not present"))
        return self._record(Call(task, model, prompt_version, prompt, key,
                                 PENDING_MODULE, None,
                                 self.availability()["note"]))

    def _record(self, call: Call) -> Call:
        self.calls.append(call)
        self._log_lines.append(json.dumps(
            {"stage": self.stage, **call.as_dict(),
             "response": call.response}, ensure_ascii=False))
        return call

    # -- reporting -----------------------------------------------------------
    def flush_log(self) -> Optional[Path]:
        if not self._log_lines:
            return None
        log_dir = self.run_dir / "llm_log"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{self.stage}.jsonl"
        path.write_text("\n".join(self._log_lines) + "\n")
        return path

    def summary(self) -> dict:
        states: dict[str, int] = {}
        for call in self.calls:
            states[call.state] = states.get(call.state, 0) + 1
        return {"stage": self.stage, "calls": len(self.calls),
                "by_state": dict(sorted(states.items())),
                "cache_root": str(self.cache_root),
                **self.availability()}


def runner(stage: str, run_dir: Path, output_root: Path, enabled: bool = True) -> Runner:
    return Runner(stage=stage, run_dir=run_dir,
                  cache_root=output_root / "llm_cache", enabled=enabled)
