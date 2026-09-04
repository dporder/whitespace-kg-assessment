"""LLM access for the chat UI, behind one seam.

CLAUDE.md requires every LLM call to go through `pipeline/llm.py`, which
resolver-builder owns and which does not exist yet. So this module tries that
import first and uses it when it is there; otherwise it takes an INTERIM PATH
straight to the anthropic SDK, keeping the same call-logging shape SPEC ground
rule 0 asks for (model, prompt version and raw response under
output/<run>/llm_log/). Everything marked INTERIM below is meant to be deleted
the day pipeline/llm.py lands: no other module in chat/ imports anthropic, so
the swap is confined to this file.

The key is read from the process environment or the gitignored .env at
config.ENV_FILE. It is never logged, printed or returned.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from . import config as ui_config

PROMPT_VERSIONS = {"chat_gate": "gate-v1", "chat_plan": "plan-v1", "chat_agent": "agent-v1"}


class LLMUnavailable(RuntimeError):
    """No key, no SDK, or the upstream refused. Callers decide how to degrade."""


@dataclass
class LLMResponse:
    """Backend-neutral shape the agent works with."""

    text: str = ""
    tool_uses: list[dict] = field(default_factory=list)   # {id, name, input}
    stop_reason: str | None = None
    model: str | None = None
    usage: dict = field(default_factory=dict)
    raw: Any = None


# --------------------------------------------------------------------------
# the seam
# --------------------------------------------------------------------------
def _pipeline_llm():
    """pipeline/llm.py if resolver-builder has landed it, else None."""
    try:
        import pipeline.llm as m           # type: ignore[import-not-found]
    except Exception:
        return None
    return m


def backend_name() -> str:
    return "pipeline.llm" if _pipeline_llm() is not None else "anthropic-sdk (interim)"


# --------------------------------------------------------------------------
# call logging, the shape SPEC 0 asks for
# --------------------------------------------------------------------------
def _log_call(task: str, model: str, payload: dict, response: Any, error: str | None = None) -> None:
    try:
        d = ui_config.llm_log_dir()
        d.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task": task,
            "model": model,
            "prompt_version": PROMPT_VERSIONS.get(task, "unknown"),
            "via": backend_name(),
            "request": payload,
            "response": response,
            "error": error,
        }
        name = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{task}-{uuid.uuid4().hex[:8]}.json"
        (d / name).write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    except Exception:
        pass                                  # logging must never break a request


# --------------------------------------------------------------------------
# INTERIM PATH: anthropic SDK
# --------------------------------------------------------------------------
_client = None
_CREATE_PARAMS: set[str] | None = None


def _secret(name: str) -> str | None:
    """Environment first, then the gitignored .env. Never logged or returned
    anywhere a caller could print it by accident."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    env_file = ui_config.pipeline_config.ENV_FILE
    if env_file.exists():
        return dotenv_values(env_file).get(name)
    return None


def _sdk_client():
    global _client
    if _client is None:
        try:
            import anthropic
        except ImportError as exc:
            raise LLMUnavailable("anthropic SDK not installed") from exc
        key = _secret("ANTHROPIC_API_KEY")
        if not key:
            raise LLMUnavailable(
                "ANTHROPIC_API_KEY not in the environment or the .env at config.ENV_FILE"
            )
        kwargs: dict[str, Any] = {"api_key": key}
        # An identity-linked key is rejected without the workspace it acts in.
        # Supply ANTHROPIC_WORKSPACE_ID in the environment or .env and this
        # starts working with no code change.
        workspace = _secret("ANTHROPIC_WORKSPACE_ID")
        if workspace:
            kwargs["default_headers"] = {"anthropic-workspace-id": workspace}
        _client = anthropic.Anthropic(**kwargs)
    return _client


def _create_params() -> set[str]:
    """Which keyword arguments this SDK version's messages.create accepts.

    anthropic 1.3.0 dropped `temperature`, so anything optional is filtered
    against the live signature rather than assumed.
    """
    global _CREATE_PARAMS
    if _CREATE_PARAMS is None:
        import inspect

        try:
            _CREATE_PARAMS = set(
                inspect.signature(_sdk_client().messages.create).parameters
            )
        except Exception:
            _CREATE_PARAMS = set()
    return _CREATE_PARAMS


def _for_sdk(payload: dict) -> dict:
    supported = _create_params()
    if not supported:
        return dict(payload)
    return {k: v for k, v in payload.items() if k in supported}


def available() -> bool:
    """A key is present and, if the key needs one, a workspace id too.

    This does not prove the upstream will accept a call; a request that is
    refused surfaces as LLMUnavailable at call time and the gate fails open.
    """
    if _pipeline_llm() is not None:
        return True
    try:
        _sdk_client()
        return True
    except LLMUnavailable:
        return False


def _normalise_sdk(msg: Any) -> LLMResponse:
    text, tool_uses = [], []
    for block in getattr(msg, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text.append(getattr(block, "text", ""))
        elif btype == "tool_use":
            tool_uses.append(
                {"id": block.id, "name": block.name, "input": dict(block.input or {})}
            )
    usage = {}
    u = getattr(msg, "usage", None)
    if u is not None:
        usage = {
            "input_tokens": getattr(u, "input_tokens", None),
            "output_tokens": getattr(u, "output_tokens", None),
        }
    return LLMResponse(
        text="".join(text),
        tool_uses=tool_uses,
        stop_reason=getattr(msg, "stop_reason", None),
        model=getattr(msg, "model", None),
        usage=usage,
        raw=msg,
    )


def _serialisable(msg: Any) -> Any:
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(msg, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                continue
    return str(msg)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def complete(
    *,
    task: str,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 2048,
    temperature: float | None = None,
) -> LLMResponse:
    """One non-streaming turn."""
    payload: dict[str, Any] = {
        "model": model,
        "system": system,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
    if temperature is not None:
        payload["temperature"] = temperature

    mod = _pipeline_llm()
    if mod is not None:
        adapted = _try_pipeline(mod, task, payload)
        if adapted is not None:
            return adapted

    try:
        msg = _sdk_client().messages.create(**_for_sdk(payload))
    except LLMUnavailable:
        raise
    except Exception as exc:
        _log_call(task, model, payload, None, error=f"{type(exc).__name__}: {exc}")
        raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
    _log_call(task, model, payload, _serialisable(msg))
    return _normalise_sdk(msg)


def stream(
    *,
    task: str,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 2048,
) -> Iterator[tuple[str, Any]]:
    """One turn, yielding ("text", delta) as it arrives then ("done", LLMResponse).

    Falls back to a single ("done", ...) when the backend cannot stream.
    """
    payload: dict[str, Any] = {
        "model": model,
        "system": system,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools

    mod = _pipeline_llm()
    if mod is not None:
        streamer: Callable | None = getattr(mod, "stream", None)
        if callable(streamer):
            try:
                for event in streamer(**payload):
                    yield event
                return
            except TypeError:
                pass                                   # signature mismatch, fall through

    try:
        client = _sdk_client()
    except LLMUnavailable:
        raise
    try:
        with client.messages.stream(**_for_sdk(payload)) as s:
            for delta in s.text_stream:
                yield ("text", delta)
            final = s.get_final_message()
    except Exception as exc:
        _log_call(task, model, payload, None, error=f"{type(exc).__name__}: {exc}")
        raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
    _log_call(task, model, payload, _serialisable(final))
    yield ("done", _normalise_sdk(final))


def _try_pipeline(mod, task: str, payload: dict) -> LLMResponse | None:
    """Best-effort adapter for a module whose signature is not yet fixed.

    pipeline/llm.py does not exist while this is written, so its call shape
    cannot be verified here. Anything that does not adapt cleanly falls back
    to the interim path rather than guessing; reconcile once it lands.
    """
    for attr in ("complete", "call", "message", "chat"):
        fn = getattr(mod, attr, None)
        if not callable(fn):
            continue
        try:
            out = fn(**payload)
        except TypeError:
            continue
        except Exception as exc:
            _log_call(task, payload["model"], payload, None, error=f"pipeline.llm: {exc}")
            raise LLMUnavailable(f"pipeline.llm.{attr} failed: {exc}") from exc
        if isinstance(out, LLMResponse):
            return out
        if isinstance(out, dict) and ("text" in out or "content" in out):
            return LLMResponse(
                text=out.get("text", ""),
                tool_uses=out.get("tool_uses", []),
                stop_reason=out.get("stop_reason"),
                model=out.get("model", payload["model"]),
                usage=out.get("usage", {}),
                raw=out,
            )
        return _normalise_sdk(out)
    return None
