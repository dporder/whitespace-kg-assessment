"""The one seam every LLM call in this repo goes through.

SPEC ground rule 0: "All LLM calls go through `pipeline/llm.py`. It reads the
key, sets the model per task from `config.py`, retries with backoff, logs every
call, and serves the replay cache." CLAUDE.md adds: keys never printed, never
logged, and determinism where the spec says deterministic, which for a
model-touching step means *replayable*, not *repeatable*.

The public contract, which `pipeline/eval/sections/stratified_audit.py` already
try-imports and pins:

    complete(task: str, prompt: str) -> str

`task` selects the model from `config.MODELS`; the return value is the model's
raw text. Two more entry points exist for callers that need them:

    structured(task, prompt, ...) -> dict     schema-shaped JSON, for candidate
                                              ranking (stage 3's residue call)
    message(...) / stream(...)                the shape `chat/llm_client.py`
                                              adapts to, so the chat UI's calls
                                              also land in the log and the cache

Every call is written under `output/<run>/llm_log/` as
`{model, prompt_version, request, response, error}`, and that same file is the
replay cache: a call whose inputs hash to an existing successful record never
reaches the API again, so a rerun of a stage is free and byte-stable. Errors
are logged too, in their own files, and never satisfy the cache.

Degradation, which matters tonight. The key in `config.ENV_FILE` is
identity-linked and the API refuses every request with

    anthropic-workspace-id is required when authenticating with an
    identity-linked API key

until `ANTHROPIC_WORKSPACE_ID` is set. So this module reads that id from the
environment or `config.ENV_FILE` and sends it as the `anthropic-workspace-id`
header whenever it is present (mirroring `chat/llm_client.py`, which already
does this), and when it is absent the first refusal trips a breaker: every
later call raises `LLMUnavailable` immediately instead of paying for another
round trip. Callers catch that and degrade, deterministic work stands, the
residue is queued. The moment the id lands, one rerun replays the cache for
everything already answered and calls only for what is still queued.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import config

# --------------------------------------------------------------------------
# tunables. Defaults live here and are read through config when the
# orchestrator adds the block, so nothing is buried but nothing is blocked.
# --------------------------------------------------------------------------
LLM_DEFAULTS = {
    "max_attempts": 4,             # total attempts per call, including the first
    "backoff_base_seconds": 1.0,   # attempt n sleeps base * 2**(n-1), jittered
    "backoff_max_seconds": 20.0,
    "max_tokens": 1024,
    "timeout_seconds": 60.0,
}


def tunables() -> dict:
    """`config.LLM` when the orchestrator adds it, else the documented defaults."""
    out = dict(LLM_DEFAULTS)
    out.update(getattr(config, "LLM", {}) or {})
    return out


# Prompt versions for the prompts this repo owns. A caller composing its own
# prompt (the eval judge, the chat agent) passes `prompt_version=` or gets
# "caller-supplied", so the log never claims a version it does not know.
PROMPT_VERSIONS = {
    "reference_residue": "refres-v1",
    "reference_hard": "refhard-v1",
    "reference_spans": "refspan-v1",
    "legislation_near_miss": "legnm-v1",
}
CALLER_SUPPLIED = "caller-supplied"

JSON_SYSTEM = ("You answer with one JSON value and nothing else. No prose, no "
               "markdown fences, no explanation outside the JSON.")


class LLMUnavailable(RuntimeError):
    """No key, no SDK, a refused request, or the breaker is open."""


class LLMResponseError(RuntimeError):
    """The call succeeded but the body was not the shape the caller asked for."""


# --------------------------------------------------------------------------
# secrets. Read on demand, held in module scope, never returned or logged.
# --------------------------------------------------------------------------
def _secret(name: str) -> Optional[str]:
    val = os.environ.get(name)
    if val:
        return val.strip() or None
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    env_file = config.ENV_FILE
    if env_file.exists():
        val = (dotenv_values(env_file) or {}).get(name)
        return val.strip() if val else None
    return None


def _scrub(text: str) -> str:
    """Belt and braces: no secret ever reaches a log file or an exception."""
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NEO4J_PASSWORD"):
        secret = _secret(name)
        if secret and len(secret) > 8 and secret in text:
            text = text.replace(secret, "***redacted***")
    return text


def workspace_id() -> Optional[str]:
    return _secret("ANTHROPIC_WORKSPACE_ID")


# --------------------------------------------------------------------------
# where the log and cache live
# --------------------------------------------------------------------------
_run_dir: Optional[Path] = None


def set_run(run: str, output_root: Optional[Path] = None) -> Path:
    """Point the log and cache at `output/<run>/llm_log/`. Stages call this once."""
    global _run_dir
    _run_dir = Path(output_root or config.OUTPUT) / run
    return _run_dir


def set_run_dir(run_dir: Path) -> Path:
    global _run_dir
    _run_dir = Path(run_dir)
    return _run_dir


def run_dir() -> Path:
    if _run_dir is not None:
        return _run_dir
    env = os.environ.get("PIPELINE_RUN")
    return Path(config.OUTPUT) / (env or "dev")


def log_dir() -> Path:
    return run_dir() / "llm_log"


_cache_enabled = True


def set_cache_enabled(enabled: bool) -> None:
    """Off makes every call hit the API. On (the default) replays."""
    global _cache_enabled
    _cache_enabled = bool(enabled)


# --------------------------------------------------------------------------
# the breaker. One permanent refusal disables the rest of the run.
# --------------------------------------------------------------------------
_breaker_reason: Optional[str] = None

# Not worth a retry: the same request will be refused again.
_PERMANENT = re.compile(
    r"anthropic-workspace-id|invalid_request_error|authentication_error|"
    r"permission_error|not_found_error|invalid x-api-key|credit balance",
    re.I,
)
# Worth disabling the whole run for: nothing this process can send will be
# accepted until a human changes something. A malformed request is permanent
# for that call but says nothing about the next one, so it is not here.
_FATAL = re.compile(
    r"anthropic-workspace-id|authentication_error|permission_error|"
    r"invalid x-api-key|credit balance",
    re.I,
)


def breaker_reason() -> Optional[str]:
    """Why calls are being refused without a round trip, or None."""
    return _breaker_reason


def reset_breaker() -> None:
    global _breaker_reason
    _breaker_reason = None


def _trip(reason: str) -> None:
    global _breaker_reason
    if _breaker_reason is None:
        _breaker_reason = _scrub(reason)


def available() -> bool:
    """A key is present, the SDK imports, and no permanent refusal has landed.

    Never proves the upstream will accept a call: that surfaces as
    LLMUnavailable at call time and trips the breaker.
    """
    if _breaker_reason is not None:
        return False
    try:
        _client()
        return True
    except LLMUnavailable:
        return False


def unavailable_reason() -> Optional[str]:
    """A one-line reason callers can record beside a queued item, or None."""
    if _breaker_reason:
        return _breaker_reason
    try:
        _client()
    except LLMUnavailable as exc:
        return str(exc)
    return None


# --------------------------------------------------------------------------
# the SDK client
# --------------------------------------------------------------------------
_sdk_client: Any = None
_create_params: Optional[set[str]] = None


def _client() -> Any:
    global _sdk_client
    if _sdk_client is None:
        try:
            import anthropic
        except ImportError as exc:                       # pragma: no cover - installed
            raise LLMUnavailable("anthropic SDK not installed") from exc
        key = _secret("ANTHROPIC_API_KEY")
        if not key:
            raise LLMUnavailable(
                "ANTHROPIC_API_KEY not in the environment or the .env at config.ENV_FILE"
            )
        kwargs: dict[str, Any] = {"api_key": key,
                                  "timeout": float(tunables()["timeout_seconds"])}
        ws = workspace_id()
        if ws:
            # An identity-linked key is refused without the workspace it acts in.
            kwargs["default_headers"] = {"anthropic-workspace-id": ws}
        _sdk_client = anthropic.Anthropic(**kwargs)
    return _sdk_client


def set_client(client: Any) -> None:
    """Inject a client. Tests use it; nothing in the pipeline does."""
    global _sdk_client, _create_params
    _sdk_client, _create_params = client, None


def _supported_params() -> set[str]:
    """Which kwargs this SDK's messages.create takes (1.3.0 dropped temperature).

    An empty set means "do not filter": either the signature could not be read,
    or it ends in **kwargs and therefore accepts everything. Filtering against a
    **kwargs signature would strip the whole payload.
    """
    global _create_params
    if _create_params is None:
        import inspect
        try:
            params = inspect.signature(_client().messages.create).parameters
            _create_params = (set() if any(p.kind is inspect.Parameter.VAR_KEYWORD
                                           for p in params.values())
                              else set(params))
        except Exception:                                 # noqa: BLE001
            _create_params = set()
    return _create_params


def _for_sdk(payload: dict) -> dict:
    supported = _supported_params()
    return dict(payload) if not supported else {k: v for k, v in payload.items()
                                                if k in supported}


# --------------------------------------------------------------------------
# the replay cache, which is the call log
# --------------------------------------------------------------------------
def cache_key(payload: dict, task: str, prompt_version: str) -> str:
    """sha1 over everything that could change the answer. Stable across runs:
    no clock, no run id, no dict-ordering leak."""
    material = json.dumps(
        {"task": task, "prompt_version": prompt_version, "payload": payload},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha1(material.encode()).hexdigest()


def _record_path(task: str, key: str) -> Path:
    return log_dir() / task / f"{key}.json"


def _write_record(path: Path, record: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    except Exception:                                     # noqa: BLE001
        pass          # logging must never take a request down with it


def _log_success(task: str, key: str, record: dict) -> None:
    _write_record(_record_path(task, key), record)


def _log_error(task: str, key: str, record: dict) -> None:
    """Errors live beside the cache, never in it: a failed call must not replay
    as an answer. Named by attempt count so a retry never overwrites evidence."""
    d = log_dir() / task / "errors"
    n = 0
    try:
        d.mkdir(parents=True, exist_ok=True)
        n = len(list(d.glob(f"{key}.error-*.json")))
    except Exception:                                     # noqa: BLE001
        pass
    _write_record(d / f"{key}.error-{n}.json", record)


def cached(task: str, key: str) -> Optional[dict]:
    """The stored record for this call, or None."""
    if not _cache_enabled:
        return None
    path = _record_path(task, key)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text())
    except Exception:                                     # noqa: BLE001
        return None
    return record if record.get("response") is not None and not record.get("error") else None


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------
@dataclass
class Completion:
    """Backend-neutral result. Field names match `chat/llm_client.LLMResponse`
    so the chat UI's adapter reads it without a shim."""
    text: str = ""
    tool_uses: list[dict] = field(default_factory=list)
    stop_reason: Optional[str] = None
    model: Optional[str] = None
    usage: dict = field(default_factory=dict)
    raw: Any = None
    cached: bool = False


def model_for(task: str) -> str:
    try:
        return config.MODELS[task]
    except KeyError as exc:
        raise ValueError(f"unknown task {task!r}; config.MODELS has "
                         f"{sorted(config.MODELS)}") from exc


def _normalise(msg: Any) -> Completion:
    text, tool_uses = [], []
    for block in getattr(msg, "content", None) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text.append(getattr(block, "text", "") or "")
        elif btype == "tool_use":
            tool_uses.append({"id": block.id, "name": block.name,
                              "input": dict(block.input or {})})
    usage: dict = {}
    u = getattr(msg, "usage", None)
    if u is not None:
        usage = {"input_tokens": getattr(u, "input_tokens", None),
                 "output_tokens": getattr(u, "output_tokens", None)}
    return Completion(text="".join(text), tool_uses=tool_uses,
                      stop_reason=getattr(msg, "stop_reason", None),
                      model=getattr(msg, "model", None), usage=usage, raw=msg)


def _serialisable(msg: Any) -> Any:
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(msg, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:                             # noqa: BLE001
                continue
    return str(msg)


def _retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status == 408 or status == 429 or status >= 500
    name = type(exc).__name__
    return name in {"APIConnectionError", "APITimeoutError", "ConnectionError",
                    "TimeoutError", "InternalServerError", "RateLimitError",
                    "APIStatusError"}


_sleep: Callable[[float], None] = time.sleep


def set_sleep(fn: Callable[[float], None]) -> None:
    """Tests replace the backoff sleep. Nothing in the pipeline does."""
    global _sleep
    _sleep = fn


def call(task: str, payload: dict, *, prompt_version: Optional[str] = None) -> Completion:
    """One logged, cached, retried turn. Everything else here funnels through it."""
    version = prompt_version or PROMPT_VERSIONS.get(task, CALLER_SUPPLIED)
    key = cache_key(payload, task, version)

    hit = cached(task, key)
    if hit is not None:
        c = _completion_from_record(hit)
        c.cached = True
        return c

    if _breaker_reason is not None:
        raise LLMUnavailable(f"llm disabled for this run: {_breaker_reason}")

    t = tunables()
    attempts = max(1, int(t["max_attempts"]))
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            client = _client()
        except LLMUnavailable as exc:
            _trip(str(exc))
            _log_error(task, key, _record(task, version, payload, None, str(exc), attempt))
            raise
        try:
            msg = client.messages.create(**_for_sdk(payload))
        except Exception as exc:                          # noqa: BLE001
            last = exc
            detail = _scrub(f"{type(exc).__name__}: {exc}")
            _log_error(task, key, _record(task, version, payload, None, detail, attempt))
            if _FATAL.search(detail):
                _trip(detail)
                raise LLMUnavailable(detail) from exc
            if _PERMANENT.search(detail) or attempt >= attempts or not _retryable(exc):
                raise LLMUnavailable(detail) from exc
            back = min(float(t["backoff_base_seconds"]) * (2 ** (attempt - 1)),
                       float(t["backoff_max_seconds"]))
            _sleep(back * (0.5 + random.random() / 2))
            continue
        out = _normalise(msg)
        _log_success(task, key, _record(task, version, payload, _serialisable(msg),
                                        None, attempt, out))
        return out
    raise LLMUnavailable(_scrub(f"{type(last).__name__}: {last}"))    # pragma: no cover


def _record(task: str, version: str, payload: dict, response: Any,
            error: Optional[str], attempt: int,
            out: Optional[Completion] = None) -> dict:
    """The log row SPEC ground rule 0 asks for: model, prompt version, request,
    response, error. `text` is lifted out so a replay never has to know the
    SDK's block shape."""
    return {
        "task": task,
        "model": payload.get("model"),
        "prompt_version": version,
        "request": payload,
        "response": response,
        "error": error,
        "attempt": attempt,
        "text": None if out is None else out.text,
        "tool_uses": [] if out is None else out.tool_uses,
        "stop_reason": None if out is None else out.stop_reason,
        "usage": {} if out is None else out.usage,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _completion_from_record(record: dict) -> Completion:
    return Completion(
        text=record.get("text") or "",
        tool_uses=list(record.get("tool_uses") or []),
        stop_reason=record.get("stop_reason"),
        model=record.get("model"),
        usage=dict(record.get("usage") or {}),
        raw=record.get("response"),
    )


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def complete(task: str, prompt: str, *, system: Optional[str] = None,
             max_tokens: Optional[int] = None,
             prompt_version: Optional[str] = None) -> str:
    """The pinned contract: task selects the model, returns the raw text.

    Positional `(task, prompt)` is what `pipeline/eval` already calls.
    """
    payload: dict[str, Any] = {
        "model": model_for(task),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens or tunables()["max_tokens"]),
    }
    if system:
        payload["system"] = system
    return call(task, payload, prompt_version=prompt_version).text


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json(raw: str) -> Any:
    """Strict-ish JSON out of a model's text: fences stripped, then the
    outermost object or array. Raises LLMResponseError rather than guessing."""
    text = (raw or "").strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except Exception:                                     # noqa: BLE001
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except Exception:                             # noqa: BLE001
                continue
    raise LLMResponseError(f"no JSON value in a {len(raw or '')}-character response")


def structured(task: str, prompt: str, *, system: Optional[str] = None,
               max_tokens: Optional[int] = None,
               prompt_version: Optional[str] = None) -> Any:
    """A schema-shaped JSON answer. The candidate-ranking call in stage 3 uses
    this: the prompt states the key order, the model emits its confidence
    before its answer, and the whole exchange is logged and replayable."""
    raw = complete(task, prompt,
                   system=f"{JSON_SYSTEM}\n{system}" if system else JSON_SYSTEM,
                   max_tokens=max_tokens, prompt_version=prompt_version)
    return parse_json(raw)


# -- the shape chat/llm_client.py adapts to --------------------------------
def message(*, model: str, system: str = "", messages: list[dict],
            tools: Optional[list[dict]] = None, max_tokens: int = 2048,
            temperature: Optional[float] = None, task: Optional[str] = None,
            prompt_version: Optional[str] = None) -> dict:
    """One turn from an explicit model and message list.

    `chat/llm_client._try_pipeline` calls this with exactly these keywords and
    reads the dict it returns, which is how the chat UI's calls come through
    this module's log and cache instead of going straight to the SDK.
    """
    payload: dict[str, Any] = {"model": model, "messages": messages,
                               "max_tokens": max_tokens}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    if temperature is not None:
        payload["temperature"] = temperature
    out = call(task or _task_for_model(model), payload, prompt_version=prompt_version)
    return {"text": out.text, "tool_uses": out.tool_uses,
            "stop_reason": out.stop_reason, "model": out.model or model,
            "usage": out.usage, "cached": out.cached}


def stream(*, model: str, system: str = "", messages: list[dict],
           tools: Optional[list[dict]] = None, max_tokens: int = 2048,
           task: Optional[str] = None,
           prompt_version: Optional[str] = None) -> Iterator[tuple[str, Any]]:
    """("text", delta) as it arrives, then ("done", Completion).

    A cached call yields its whole text as one delta, which is what makes a
    replayed chat turn free and identical.
    """
    payload: dict[str, Any] = {"model": model, "messages": messages,
                               "max_tokens": max_tokens}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    resolved = task or _task_for_model(model)
    version = prompt_version or PROMPT_VERSIONS.get(resolved, CALLER_SUPPLIED)
    key = cache_key(payload, resolved, version)

    hit = cached(resolved, key)
    if hit is not None:
        out = _completion_from_record(hit)
        out.cached = True
        if out.text:
            yield ("text", out.text)
        yield ("done", out)
        return

    if _breaker_reason is not None:
        raise LLMUnavailable(f"llm disabled for this run: {_breaker_reason}")

    try:
        client = _client()
    except LLMUnavailable as exc:
        _trip(str(exc))
        _log_error(resolved, key, _record(resolved, version, payload, None, str(exc), 1))
        raise
    try:
        with client.messages.stream(**_for_sdk(payload)) as s:
            for delta in s.text_stream:
                yield ("text", delta)
            final = s.get_final_message()
    except Exception as exc:                              # noqa: BLE001
        detail = _scrub(f"{type(exc).__name__}: {exc}")
        _log_error(resolved, key, _record(resolved, version, payload, None, detail, 1))
        if _FATAL.search(detail):
            _trip(detail)
        raise LLMUnavailable(detail) from exc
    out = _normalise(final)
    _log_success(resolved, key, _record(resolved, version, payload,
                                        _serialisable(final), None, 1, out))
    yield ("done", out)


def _task_for_model(model: str) -> str:
    """A caller that names no task gets one honest bucket.

    Several tasks share a model (four of `config.MODELS` are Haiku 4.5), so
    guessing the task from the model would stamp the log with a task the caller
    never asked for. `unattributed` says exactly what is known.
    """
    return "unattributed"
