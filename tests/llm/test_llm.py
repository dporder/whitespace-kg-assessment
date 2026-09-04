"""pipeline/llm.py: the pinned contract, the log, the cache, the degradation.

No test here reaches the network. A fake client stands in for the SDK, which is
also how the "what happens when the workspace id is missing" case is pinned:
the real API returns exactly the message asserted below, verified by hand
against the live key before this test was written.
"""
from __future__ import annotations

import json

import pytest

from pipeline import llm


class FakeBlock:
    def __init__(self, text):
        self.type, self.text = "text", text


class FakeUsage:
    input_tokens, output_tokens = 11, 7


class FakeMessage:
    def __init__(self, text, model="claude-haiku-4-5"):
        self.content = [FakeBlock(text)]
        self.stop_reason = "end_turn"
        self.model = model
        self.usage = FakeUsage()

    def model_dump(self):
        return {"content": [{"type": "text", "text": self.content[0].text}],
                "stop_reason": self.stop_reason, "model": self.model}


class FakeMessages:
    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.calls = []

    def create(self, **payload):
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return FakeMessage(self.replies.pop(0) if self.replies else "ok")


class FakeClient:
    def __init__(self, replies=None, error=None):
        self.messages = FakeMessages(replies, error)


class Refused(Exception):
    """Shaped like anthropic.BadRequestError for the workspace-id refusal."""
    status_code = 400

    def __str__(self):
        return ("Error code: 400 - {'type': 'error', 'error': {'type': "
                "'invalid_request_error', 'message': 'anthropic-workspace-id is "
                "required when authenticating with an identity-linked API key; "
                "send the id of the workspace this request acts in.'}}")


class Flaky(Exception):
    status_code = 500

    def __str__(self):
        return "Error code: 500 - overloaded"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Each test gets its own log dir, its own breaker and no real sleeping."""
    llm.set_run_dir(tmp_path / "run")
    llm.reset_breaker()
    llm.set_cache_enabled(True)
    llm.set_sleep(lambda _s: None)
    monkeypatch.setattr(llm, "_sdk_client", None, raising=False)
    monkeypatch.setattr(llm, "_create_params", set(), raising=False)
    yield
    llm.set_client(None)
    llm.reset_breaker()


def test_complete_is_task_and_prompt_positionally():
    """The signature pipeline/eval/sections/stratified_audit.py already calls."""
    llm.set_client(FakeClient(["[]"]))
    assert llm.complete("eval_judge", "check these") == "[]"


def test_task_selects_the_model_from_config():
    import config
    client = FakeClient(["fine"])
    llm.set_client(client)
    llm.complete("reference_residue", "rank these")
    assert client.messages.calls[0]["model"] == config.MODELS["reference_residue"]


def test_unknown_task_is_a_programming_error():
    with pytest.raises(ValueError) as exc:
        llm.model_for("no_such_task")
    assert "config.MODELS" in str(exc.value)


def test_every_call_is_logged_with_the_spec_shape():
    llm.set_client(FakeClient(["hello"]))
    llm.complete("eval_judge", "hi")
    files = list((llm.log_dir() / "eval_judge").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    for field in ("model", "prompt_version", "request", "response", "error"):
        assert field in record
    assert record["error"] is None
    assert record["text"] == "hello"


def test_second_identical_call_replays_from_cache():
    client = FakeClient(["once"])
    llm.set_client(client)
    assert llm.complete("eval_judge", "same prompt") == "once"
    assert llm.complete("eval_judge", "same prompt") == "once"
    assert len(client.messages.calls) == 1, "cache hit still reached the API"


def test_a_changed_prompt_is_a_different_cache_key():
    client = FakeClient(["a", "b"])
    llm.set_client(client)
    llm.complete("eval_judge", "prompt one")
    llm.complete("eval_judge", "prompt two")
    assert len(client.messages.calls) == 2


def test_a_changed_prompt_version_invalidates_the_cache():
    client = FakeClient(["a", "b"])
    llm.set_client(client)
    llm.complete("eval_judge", "p", prompt_version="v1")
    llm.complete("eval_judge", "p", prompt_version="v2")
    assert len(client.messages.calls) == 2


def test_cache_key_is_stable_across_dict_ordering():
    a = llm.cache_key({"model": "m", "max_tokens": 4}, "t", "v1")
    b = llm.cache_key({"max_tokens": 4, "model": "m"}, "t", "v1")
    assert a == b


def test_missing_workspace_id_degrades_cleanly_and_trips_the_breaker():
    client = FakeClient(error=Refused())
    llm.set_client(client)
    with pytest.raises(llm.LLMUnavailable) as exc:
        llm.complete("reference_residue", "rank")
    assert "anthropic-workspace-id" in str(exc.value)
    assert llm.breaker_reason() is not None
    assert llm.available() is False
    # The second call must not pay for another round trip.
    with pytest.raises(llm.LLMUnavailable):
        llm.complete("reference_residue", "rank something else")
    assert len(client.messages.calls) == 1


def test_a_refusal_is_logged_as_an_error_and_never_cached():
    llm.set_client(FakeClient(error=Refused()))
    with pytest.raises(llm.LLMUnavailable):
        llm.complete("reference_residue", "rank")
    errors = list((llm.log_dir() / "reference_residue" / "errors").glob("*.json"))
    assert len(errors) == 1
    assert json.loads(errors[0].read_text())["error"]
    assert not list((llm.log_dir() / "reference_residue").glob("*.json"))


def test_transient_failures_retry_with_backoff_then_give_up():
    slept: list[float] = []
    llm.set_sleep(slept.append)
    client = FakeClient(error=Flaky())
    llm.set_client(client)
    with pytest.raises(llm.LLMUnavailable):
        llm.complete("eval_judge", "hi")
    attempts = llm.tunables()["max_attempts"]
    assert len(client.messages.calls) == attempts
    assert len(slept) == attempts - 1
    assert slept == sorted(slept), "backoff did not grow"
    assert llm.breaker_reason() is None, "a 500 must not disable the run"


def test_structured_parses_json_out_of_a_fenced_reply():
    llm.set_client(FakeClient(['```json\n{"answer": "NONE", "confidence": 0.2}\n```']))
    out = llm.structured("reference_residue", "rank")
    assert out == {"answer": "NONE", "confidence": 0.2}


def test_structured_raises_rather_than_guessing():
    llm.set_client(FakeClient(["I think it is clause 3."]))
    with pytest.raises(llm.LLMResponseError):
        llm.structured("reference_residue", "rank")


def test_workspace_header_is_sent_when_the_id_is_present(monkeypatch):
    seen = {}

    class Anthropic:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.messages = FakeMessages(["ok"])

    import sys
    import types
    module = types.ModuleType("anthropic")
    module.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    llm.set_client(None)
    monkeypatch.setattr(llm, "_sdk_client", None, raising=False)
    llm.complete("eval_judge", "hi")
    assert seen["default_headers"] == {"anthropic-workspace-id": "wrkspc_test"}


def test_no_secret_reaches_the_log(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value-1234567890")
    llm.set_client(FakeClient(["ok"]))
    llm.complete("eval_judge", "hi")
    blob = "".join(p.read_text() for p in llm.log_dir().rglob("*.json"))
    assert "sk-secret-value-1234567890" not in blob


def test_message_returns_the_shape_chat_adapts_to():
    llm.set_client(FakeClient(["hi there"]))
    out = llm.message(model="claude-haiku-4-5", system="sys",
                      messages=[{"role": "user", "content": "yo"}], max_tokens=64)
    assert out["text"] == "hi there"
    assert out["tool_uses"] == []
    assert set(out) >= {"text", "tool_uses", "stop_reason", "model", "usage"}


def test_stream_replays_a_cached_turn_without_calling():
    client = FakeClient(["streamed"])
    llm.set_client(client)
    llm.message(model="claude-haiku-4-5", messages=[{"role": "user", "content": "q"}],
                max_tokens=64)
    events = list(llm.stream(model="claude-haiku-4-5",
                             messages=[{"role": "user", "content": "q"}], max_tokens=64))
    assert events[0][0] == "text" and events[0][1] == "streamed"
    done = events[-1]
    assert done[0] == "done" and done[1].text == "streamed" and done[1].cached
    assert len(client.messages.calls) == 1


# --------------------------------------------------------------------------
# scrubbing. Every test here fails if `scrub` stops being applied.
# --------------------------------------------------------------------------
class EchoesTheSecret(Exception):
    """The shape a 400 takes when the upstream quotes what you sent it."""

    status_code = 400

    def __init__(self, secret):
        self.secret = secret

    def __str__(self):
        return ("Error code: 400 - {'error': {'type': 'invalid_request_error', "
                f"'message': \'workspace {self.secret} is not accessible with this "
                "key\'}}")


def test_a_refusal_quoting_the_workspace_id_is_scrubbed_from_the_log(monkeypatch):
    """A wrong workspace id comes back quoted in the 400, and that error text
    travels into refs/report.json and onto the terminal."""
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_0123456789abcdef")
    llm.set_client(FakeClient(error=EchoesTheSecret("wrkspc_0123456789abcdef")))
    with pytest.raises(llm.LLMUnavailable) as exc:
        llm.complete("reference_residue", "rank")
    assert "wrkspc_0123456789abcdef" not in str(exc.value)
    assert "***redacted:ANTHROPIC_WORKSPACE_ID***" in str(exc.value)
    blob = "".join(p.read_text() for p in llm.log_dir().rglob("*.json"))
    assert "wrkspc_0123456789abcdef" not in blob


def test_a_refusal_quoting_the_api_key_is_scrubbed_from_the_log(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-0123456789")
    llm.set_client(FakeClient(error=EchoesTheSecret("sk-ant-secret-0123456789")))
    with pytest.raises(llm.LLMUnavailable) as exc:
        llm.complete("reference_residue", "rank")
    assert "sk-ant-secret-0123456789" not in str(exc.value)
    blob = "".join(p.read_text() for p in llm.log_dir().rglob("*.json"))
    assert "sk-ant-secret-0123456789" not in blob


def test_scrub_covers_every_secret_the_pipeline_reads(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-key-value-here")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_abcd")
    monkeypatch.setenv("NEO4J_PASSWORD", "hunter2-password")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-value")
    text = ("key sk-key-value-here workspace wrkspc_abcd password hunter2-password "
            "openai sk-openai-value")
    cleaned = llm.scrub(text)
    for secret in ("sk-key-value-here", "wrkspc_abcd", "hunter2-password",
                   "sk-openai-value"):
        assert secret not in cleaned
    assert "ANTHROPIC_WORKSPACE_ID" in cleaned


def test_a_short_secret_is_still_redacted(monkeypatch):
    """The old guard was `len > 8`, which left a short workspace id in the clear."""
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "ws01")
    assert "ws01" not in llm.scrub("workspace ws01 refused")


def test_a_one_character_value_is_not_redacted(monkeypatch):
    """Redacting a single character would blank out ordinary log text."""
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "a")
    assert llm.scrub("a plain sentence") == "a plain sentence"
