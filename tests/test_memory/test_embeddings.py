"""Tests for sentinel.memory.embeddings.EmbeddingClient."""

from __future__ import annotations

import numpy as np
import pytest

from sentinel.memory.embeddings import EmbeddingClient

DIMS = 8  # small dimension — keeps tests fast without real model


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
