"""The embedding client: batched, cached, key-safe, honest when blocked.

`text-embedding-3-large` through OpenAI, with the key read from the process
environment or the gitignored `.env` named by `config.ENV_FILE`. The key is
never printed, never logged and never written into any output; the only thing
recorded about it is whether one was found.

Three properties matter more than the call itself.

**Vectors live outside the graph.** They are written to a content-addressed store
under `output/embeddings_cache/<model>/<sha1>.json` and referenced from the index
by node id, so re-embedding on a new model never rewrites a graph node, and a
sovereign deployment swapping in an in-boundary model is a re-embed rather than a
migration (DESIGN stage 6).

**The cache is keyed on the input, not on the run.** `sha1(model|text)` means a
rerun over unchanged text costs nothing, and the store is shared across runs. It
sits beside the run directories rather than inside one for exactly that reason.

**Blocked is not zero.** The OpenAI key in this environment has no credit
tonight, so `embed` returns a result that says which vectors it has and which it
does not, and the caller writes the missing ones into `pending.json` with the
reason. Nothing invents a vector, and no `EmbeddingRecord` is emitted with a
`vector_ref` pointing at a file that does not exist.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config

# The API accepts far more per request; this keeps one failure from costing a
# whole part's worth of work and keeps request bodies a readable size.
BATCH_SIZE = 96

PENDING_NO_KEY = "pending_credentials_no_key"
PENDING_REFUSED = "pending_credentials_refused"
PENDING_NO_CLIENT = "pending_openai_client"
DISABLED = "disabled"
FAILED = "failed"

_CREDENTIAL_MARKERS = ("authentication", "unauthorized", "unauthorised", "api key",
                       "credit", "quota", "billing", "insufficient", "401", "403", "429")


def text_key(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}|{text}".encode()).hexdigest()


def _api_key() -> Optional[str]:
    """Process environment first, then the gitignored .env. Never returned to
    any caller that logs, and never included in any output."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    env_file: Path = config.ENV_FILE
    if not env_file.exists():
        return None
    try:
        from dotenv import dotenv_values                   # noqa: PLC0415
        return dotenv_values(env_file).get("OPENAI_API_KEY") or None
    except Exception:                                      # noqa: BLE001
        for line in env_file.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENAI_API_KEY":
                return value.strip().strip("'\"") or None
    return None


@dataclass
class EmbedResult:
    vectors: dict[str, str] = field(default_factory=dict)    # text -> vector_ref
    dims: dict[str, int] = field(default_factory=dict)       # text -> dimensions
    missing: dict[str, str] = field(default_factory=dict)    # text -> reason
    cache_hits: int = 0
    api_calls: int = 0
    embedded: int = 0
    note: str = ""


@dataclass
class Embedder:
    """Content-addressed embedding store plus the provider call."""
    output_root: Path
    model: str = config.EMBEDDING_MODEL
    enabled: bool = True

    @property
    def store(self) -> Path:
        return self.output_root / "embeddings_cache" / self.model

    def vector_ref(self, key: str) -> str:
        """The value that goes on an EmbeddingRecord: a path relative to
        `output/`, so the record is portable across checkouts."""
        return f"embeddings_cache/{self.model}/{key}.json"

    def path_for(self, key: str) -> Path:
        return self.store / f"{key}.json"

    def cached(self, text: str) -> Optional[tuple[str, int]]:
        key = text_key(self.model, text)
        path = self.path_for(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
            return self.vector_ref(key), len(payload["vector"])
        except Exception:                                  # noqa: BLE001
            return None

    def write_vector(self, text: str, vector: list[float]) -> tuple[str, int]:
        key = text_key(self.model, text)
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": self.model, "text_sha1": key,
                                    "dimensions": len(vector), "vector": vector}) + "\n")
        return self.vector_ref(key), len(vector)

    # -- availability --------------------------------------------------------
    def availability(self) -> dict:
        try:
            import openai                                  # noqa: PLC0415,F401
            client_ok, client_note = True, "openai client importable"
        except Exception as exc:                           # noqa: BLE001
            client_ok, client_note = False, f"openai client unavailable: {exc}"
        has_key = _api_key() is not None
        return {"model": self.model, "client_available": client_ok,
                "client_note": client_note, "api_key_present": has_key,
                "key_source": ("environment or config.ENV_FILE" if has_key
                               else "not found; never printed either way"),
                "store": str(self.store)}

    # -- the call ------------------------------------------------------------
    def embed(self, texts: list[str]) -> EmbedResult:
        """Vectors for every distinct text, cache first, provider second."""
        result = EmbedResult()
        wanted: list[str] = []
        for text in dict.fromkeys(texts):                  # distinct, order kept
            hit = self.cached(text)
            if hit is not None:
                result.vectors[text], result.dims[text] = hit
                result.cache_hits += 1
            else:
                wanted.append(text)
        if not wanted:
            result.note = "every text served from the embedding cache"
            return result
        if not self.enabled:
            result.missing = {t: DISABLED for t in wanted}
            result.note = "--no-embed: the provider was not called"
            return result

        availability = self.availability()
        if not availability["client_available"]:
            result.missing = {t: PENDING_NO_CLIENT for t in wanted}
            result.note = availability["client_note"]
            return result
        if not availability["api_key_present"]:
            result.missing = {t: PENDING_NO_KEY for t in wanted}
            result.note = ("no OPENAI_API_KEY in the environment or config.ENV_FILE; "
                           "vectors are pending, and one rerun completes them")
            return result

        from openai import OpenAI                          # noqa: PLC0415
        client = OpenAI(api_key=_api_key())
        for start in range(0, len(wanted), BATCH_SIZE):
            batch = wanted[start:start + BATCH_SIZE]
            try:
                response = client.embeddings.create(model=self.model, input=batch)
                result.api_calls += 1
            except Exception as exc:                       # noqa: BLE001
                text_of = f"{type(exc).__name__}: {exc}".lower()
                reason = (PENDING_REFUSED
                          if any(m in text_of for m in _CREDENTIAL_MARKERS) else FAILED)
                for text in batch:
                    result.missing[text] = reason
                result.note = f"{type(exc).__name__}: {exc}"[:300]
                continue
            for text, item in zip(batch, response.data):
                ref, dims = self.write_vector(text, list(item.embedding))
                result.vectors[text], result.dims[text] = ref, dims
                result.embedded += 1
        if not result.note:
            result.note = (f"{result.embedded} embedded, {result.cache_hits} "
                           f"served from cache")
        return result


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


def load_vector(output_root: Path, vector_ref: str) -> Optional[list[float]]:
    path = output_root / vector_ref
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())["vector"]
    except Exception:                                      # noqa: BLE001
        return None
