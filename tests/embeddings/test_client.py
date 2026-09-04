"""The embedding client: cache, batching, secrets and honest blockage."""
from __future__ import annotations

import json

import config
from pipeline.embeddings import client as client_mod
from tests.embeddings.conftest import DIMS


def embedder(tmp_path, **kw):
    return client_mod.Embedder(output_root=tmp_path, **kw)


# --------------------------------------------------------------- the cache


def test_a_vector_is_stored_by_content_and_referenced_by_a_relative_path(
        tmp_path, fake_openai):
    e = embedder(tmp_path)
    result = e.embed(["the register shall be kept"])
    ref = result.vectors["the register shall be kept"]
    assert ref.startswith(f"embeddings_cache/{config.EMBEDDING_MODEL}/")
    assert (tmp_path / ref).exists()
    payload = json.loads((tmp_path / ref).read_text())
    assert payload["dimensions"] == DIMS
    assert payload["model"] == config.EMBEDDING_MODEL


def test_a_second_run_over_unchanged_text_calls_nothing(tmp_path, fake_openai):
    first = embedder(tmp_path)
    first.embed(["alpha", "beta"])
    assert len(fake_openai.instances[-1].calls) == 1

    calls_before = sum(len(i.calls) for i in fake_openai.instances)
    second = embedder(tmp_path)
    result = second.embed(["alpha", "beta"])
    assert sum(len(i.calls) for i in fake_openai.instances) == calls_before
    assert result.cache_hits == 2
    assert result.embedded == 0
    assert result.api_calls == 0


def test_the_cache_is_shared_across_runs_not_scoped_to_one(tmp_path, fake_openai):
    """It sits beside the run directories, so a new run costs nothing for text
    that has not changed."""
    embedder(tmp_path).embed(["alpha"])
    store = tmp_path / "embeddings_cache" / config.EMBEDDING_MODEL
    assert store.is_dir() and list(store.glob("*.json"))
    assert not (tmp_path / "dev").exists()


def test_distinct_texts_are_embedded_once_each(tmp_path, fake_openai):
    e = embedder(tmp_path)
    e.embed(["same", "same", "other"])
    _model, sent = fake_openai.instances[-1].calls[0]
    assert sent == ["same", "other"]


def test_texts_are_batched(tmp_path, fake_openai, monkeypatch):
    monkeypatch.setattr(client_mod, "BATCH_SIZE", 3)
    e = embedder(tmp_path)
    e.embed([f"text {i}" for i in range(7)])
    calls = fake_openai.instances[-1].calls
    assert [len(sent) for _m, sent in calls] == [3, 3, 1]


# ------------------------------------------------------------ blocked paths


def test_no_key_means_pending_not_a_fabricated_vector(tmp_path, fake_openai,
                                                      monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "nonexistent.env")
    result = embedder(tmp_path).embed(["alpha"])
    assert result.vectors == {}
    assert result.missing["alpha"] == client_mod.PENDING_NO_KEY
    assert "pending" in result.note


def test_a_credential_refusal_is_pending_not_failed(tmp_path, fake_openai):
    fake_openai.raises_class = RuntimeError(
        "Error code: 429 - insufficient_quota: You exceeded your current quota")
    result = embedder(tmp_path).embed(["alpha"])
    assert result.missing["alpha"] == client_mod.PENDING_REFUSED


def test_an_unrelated_error_is_failed_not_pending(tmp_path, fake_openai):
    fake_openai.raises_class = RuntimeError("connection reset by peer")
    result = embedder(tmp_path).embed(["alpha"])
    assert result.missing["alpha"] == client_mod.FAILED


def test_no_embed_builds_nothing_and_calls_nothing(tmp_path, fake_openai):
    result = embedder(tmp_path, enabled=False).embed(["alpha"])
    assert result.missing["alpha"] == client_mod.DISABLED
    assert fake_openai.instances == []


# ------------------------------------------------------------------ secrets


def test_the_key_never_reaches_any_output(tmp_path, fake_openai, monkeypatch):
    """`availability()` is written into summary.json, so it must say whether a
    key was found and nothing else about it."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value-do-not-leak")
    e = embedder(tmp_path)
    availability = e.availability()
    assert availability["api_key_present"] is True
    assert "sk-secret-value-do-not-leak" not in json.dumps(availability)
    e.embed(["alpha"])
    for path in tmp_path.rglob("*.json"):
        assert "sk-secret-value-do-not-leak" not in path.read_text()


def test_the_key_is_read_from_the_env_file_when_the_environment_has_none(
        tmp_path, fake_openai, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-from-the-dotenv-file\n")
    monkeypatch.setattr(config, "ENV_FILE", env)
    assert embedder(tmp_path).availability()["api_key_present"] is True


# ------------------------------------------------------------------ cosine


def test_cosine_is_one_for_identical_and_zero_for_orthogonal():
    assert client_mod.cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert client_mod.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert client_mod.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
