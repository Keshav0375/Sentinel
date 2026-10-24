"""Tests for sentinel.memory.embeddings.EmbeddingClient."""

from __future__ import annotations

import contextvars
import math
import sys
import threading
import time
import types

import numpy as np
import pytest

from sentinel.config import Settings
from sentinel.memory import embeddings
from sentinel.memory.embeddings import EmbeddingClient

DIMS = 8  # small dimension — keeps tests fast without real model
REAL_DIMS = 384  # all-MiniLM-L6-v2; matches the VECTOR(384) columns


class _FakeModel:
    """Fake sentence-transformers model for testing.

    Returns deterministic vectors based on text content.
    Tracks how many encode() calls have been made to verify caching.
    """

    def __init__(self, dims: int = DIMS) -> None:
        self.dims = dims
        self.encode_calls: int = 0

    def encode(
        self,
        texts: list[str],
        *,
        convert_to_numpy: bool = True,
        show_progress_bar: bool = False,
    ) -> np.ndarray:
        self.encode_calls += 1
        result = []
        for text in texts:
            # Deterministic: vector[0] is a normalized hash of the text
            vec = np.zeros(self.dims, dtype=np.float32)
            vec[0] = float(abs(hash(text)) % 1000) / 1000.0
            vec[1] = float(len(text) % 100) / 100.0
            result.append(vec)
        return np.array(result, dtype=np.float32)


# ── Construction ──────────────────────────────────────────────────────────────


def test_embedding_client_constructs() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    assert client.cache_size == 0


def test_embedding_client_initial_cache_empty() -> None:
    client = EmbeddingClient(_FakeModel())
    assert client.cache_size == 0


# ── embed() ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embed_returns_list_of_float() -> None:
    client = EmbeddingClient(_FakeModel())
    result = await client.embed("api-gateway error spike")
    assert isinstance(result, list)
    assert all(isinstance(v, float) for v in result)


@pytest.mark.asyncio
async def test_embed_returns_correct_dimension() -> None:
    client = EmbeddingClient(_FakeModel(dims=DIMS))
    result = await client.embed("some symptom text")
    assert len(result) == DIMS


@pytest.mark.asyncio
async def test_embed_deterministic() -> None:
    client = EmbeddingClient(_FakeModel())
    r1 = await client.embed("connection pool exhausted")
    r2 = await client.embed("connection pool exhausted")
    assert r1 == r2


# ── embed_batch() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embed_batch_returns_correct_count() -> None:
    client = EmbeddingClient(_FakeModel())
    texts = ["text one", "text two", "text three"]
    results = await client.embed_batch(texts)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_embed_batch_each_entry_is_correct_dim() -> None:
    client = EmbeddingClient(_FakeModel(dims=DIMS))
    results = await client.embed_batch(["a", "b", "c"])
    for vec in results:
        assert len(vec) == DIMS


@pytest.mark.asyncio
async def test_embed_batch_empty_input() -> None:
    client = EmbeddingClient(_FakeModel())
    result = await client.embed_batch([])
    assert result == []


@pytest.mark.asyncio
async def test_embed_batch_preserves_order() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    texts = ["alpha", "beta", "gamma"]
    results = await client.embed_batch(texts)
    # Each result should be different (since texts differ)
    assert results[0] != results[1]
    assert results[1] != results[2]


@pytest.mark.asyncio
async def test_embed_batch_with_duplicate_texts() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    # "a" appears twice — should only be embedded once
    results = await client.embed_batch(["a", "b", "a"])
    assert results[0] == results[2]  # same text → same vector
    assert len(results) == 3


# ── Caching ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embed_caches_result() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    await client.embed("cache me")
    assert client.cache_size == 1


@pytest.mark.asyncio
async def test_embed_twice_only_calls_model_once() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    text = "NullPointerException in AuthMiddleware"
    await client.embed(text)
    await client.embed(text)
    assert model.encode_calls == 1


@pytest.mark.asyncio
async def test_embed_batch_caches_each_unique_text() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    await client.embed_batch(["x", "y", "z"])
    assert client.cache_size == 3


@pytest.mark.asyncio
async def test_embed_batch_only_encodes_uncached_texts() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    # First batch: encode 2 texts
    await client.embed_batch(["a", "b"])
    assert model.encode_calls == 1
    # Second batch: "a" is cached, only "c" is new
    await client.embed_batch(["a", "c"])
    assert model.encode_calls == 2
    assert client.cache_size == 3


@pytest.mark.asyncio
async def test_embed_batch_deduplicates_within_call() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    # All same text — should only call encode once with 1 unique item
    await client.embed_batch(["same", "same", "same"])
    assert model.encode_calls == 1
    assert client.cache_size == 1


@pytest.mark.asyncio
async def test_embed_result_same_whether_called_via_embed_or_batch() -> None:
    client = EmbeddingClient(_FakeModel())
    text = "error rate threshold breached"
    single = await client.embed(text)
    batch = await client.embed_batch([text])
    assert single == batch[0]


@pytest.mark.asyncio
async def test_cross_call_cache_hit() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    # Embed individually first, then in a batch — batch should hit cache
    vec_single = await client.embed("shared text")
    results = await client.embed_batch(["other text", "shared text"])
    assert results[1] == vec_single
    assert model.encode_calls == 2  # 1 for embed(), 1 for the new "other text"


# ── cache_size and clear_cache ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_size_reflects_unique_texts() -> None:
    client = EmbeddingClient(_FakeModel())
    await client.embed_batch(["p", "q", "r", "p"])
    assert client.cache_size == 3  # "p" only counted once


@pytest.mark.asyncio
async def test_clear_cache_resets_size() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    await client.embed("some text")
    assert client.cache_size == 1
    client.clear_cache()
    assert client.cache_size == 0


@pytest.mark.asyncio
async def test_clear_cache_forces_re_encode() -> None:
    model = _FakeModel()
    client = EmbeddingClient(model)
    await client.embed("text")
    assert model.encode_calls == 1
    client.clear_cache()
    await client.embed("text")
    assert model.encode_calls == 2


# ── Off the event loop ────────────────────────────────────────────────────────

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)


class _ThreadRecordingModel(_FakeModel):
    """Records the thread and the trace contextvar each encode() call ran with."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[int] = []
        self.trace_ids: list[str | None] = []

    def encode(self, texts: list[str], **kwargs: bool) -> np.ndarray:
        self.threads.append(threading.get_ident())
        self.trace_ids.append(_trace_id.get())
        return super().encode(texts, **kwargs)


async def test_embed_encodes_off_the_event_loop_thread() -> None:
    model = _ThreadRecordingModel()
    await EmbeddingClient(model).embed("off the loop")
    assert model.threads and model.threads[0] != threading.get_ident()


async def test_embed_carries_contextvars_into_the_worker_thread() -> None:
    # asyncio.to_thread copies the context (run_in_executor would not) — the active
    # tracing span must still be visible while the model encodes.
    model = _ThreadRecordingModel()
    token = _trace_id.set("trace-123")
    try:
        await EmbeddingClient(model).embed("traced text")
    finally:
        _trace_id.reset(token)
    assert model.trace_ids == ["trace-123"]


# ── from_model_name(): one model load per process ─────────────────────────────


@pytest.fixture
def fake_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Swap in a fake ``sentence_transformers`` module and an empty model cache.

    Returns the list of model names the fake SentenceTransformer was constructed with.
    """
    loads: list[str] = []

    class _FakeSentenceTransformer(_FakeModel):
        def __init__(self, model_name: str, revision: str | None = None) -> None:
            super().__init__()
            time.sleep(0.05)  # widen the race window for the concurrency test
            loads.append(model_name)

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(embeddings, "_MODEL_CACHE", {})
    return loads


def test_from_model_name_loads_the_model_once(fake_sentence_transformers: list[str]) -> None:
    first = EmbeddingClient.from_model_name("fake-model")
    second = EmbeddingClient.from_model_name("fake-model")
    assert fake_sentence_transformers == ["fake-model"]
    assert first._model is second._model


def test_from_model_name_clients_keep_separate_text_caches(
    fake_sentence_transformers: list[str],
) -> None:
    first = EmbeddingClient.from_model_name("fake-model")
    second = EmbeddingClient.from_model_name("fake-model")
    first._cache["x"] = [1.0]
    assert second.cache_size == 0


def test_from_model_name_caches_per_model_name(fake_sentence_transformers: list[str]) -> None:
    EmbeddingClient.from_model_name("model-a")
    EmbeddingClient.from_model_name("model-b")
    EmbeddingClient.from_model_name("model-a")
    assert fake_sentence_transformers == ["model-a", "model-b"]


def test_from_model_name_pins_the_baked_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str | None]] = []

    class _RecordingSentenceTransformer(_FakeModel):
        def __init__(self, model_name: str, revision: str | None = None) -> None:
            super().__init__()
            calls.append((model_name, revision))

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _RecordingSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(embeddings, "_MODEL_CACHE", {})
    EmbeddingClient.from_model_name("all-MiniLM-L6-v2")
    EmbeddingClient.from_model_name("other-model")
    assert calls == [
        ("all-MiniLM-L6-v2", "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"),
        ("other-model", None),
    ]


def test_from_model_name_concurrent_first_calls_load_once(
    fake_sentence_transformers: list[str],
) -> None:
    clients: list[EmbeddingClient] = []
    threads = [
        threading.Thread(target=lambda: clients.append(EmbeddingClient.from_model_name("racy")))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake_sentence_transformers == ["racy"]
    assert len({id(c._model) for c in clients}) == 1


# ── Real model (all-MiniLM-L6-v2, cached locally / in CI) ─────────────────────


@pytest.fixture
def real_client() -> EmbeddingClient:
    """A fresh client on the configured model — shares the process-wide loaded model."""
    return EmbeddingClient.from_model_name(
        Settings.model_fields["sentinel_embedding_model"].default
    )


def _dot(a: list[float], b: list[float]) -> float:
    # Assertion helper only — production similarity is pgvector <=>, never Python.
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_real_embed_returns_384_floats(real_client: EmbeddingClient) -> None:
    vec = await real_client.embed("api-gateway 5xx spike after deploy")
    assert len(vec) == REAL_DIMS
    assert all(type(v) is float for v in vec)


async def test_real_embed_is_deterministic(real_client: EmbeddingClient) -> None:
    text = "connection pool exhausted on payments-db"
    first = await real_client.embed(text)
    real_client.clear_cache()  # force a second inference, not a cache hit
    second = await real_client.embed(text)
    assert first == second


async def test_real_embed_vectors_are_unit_length(real_client: EmbeddingClient) -> None:
    vec = await real_client.embed("memory leak in checkout-service")
    assert math.isclose(math.sqrt(_dot(vec, vec)), 1.0, abs_tol=1e-4)


async def test_real_embed_similar_texts_are_closer(real_client: EmbeddingClient) -> None:
    anchor, similar, dissimilar = await real_client.embed_batch(
        [
            "database connection pool exhausted, requests timing out",
            "service cannot get a DB connection, the pool is saturated",
            "the frontend logo colour was changed to blue",
        ]
    )
    assert _dot(anchor, similar) > _dot(anchor, dissimilar)
