"""Embedding client — wraps sentence-transformers for local inference.

This module only produces embeddings. Similarity is computed in PostgreSQL with the
pgvector ``<=>`` (cosine distance) operator by the memory modules — never in Python.

Vectors are returned exactly as sentence-transformers gives them. The default
all-MiniLM-L6-v2 ends in a ``Normalize`` layer, so its 384-float vectors are already
unit length; nothing here re-normalizes, and pgvector's cosine distance does not need it.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

# One model instance per name per process: loading costs seconds and ~90 MB, and every
# caller (app lifespan, eval runner, migration 002) asks for the same configured model.
_MODEL_CACHE: dict[str, Any] = {}
_MODEL_LOCK = threading.Lock()

# Hugging Face Hub commit of each model the image bakes (Dockerfile builder stage). The pod runs
# with HF_HUB_OFFLINE=1 and a cache loaded by commit (no refs/main), so the runtime asks for the
# same commit.
PINNED_REVISIONS: dict[str, str] = {
    "all-MiniLM-L6-v2": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
}


def _load_model(model_name: str) -> Any:
    """Return the process-wide SentenceTransformer for *model_name*, loading it once.

    The lock is held across the load so concurrent first calls never load it twice.
    A pinned model id loads its PINNED_REVISIONS commit; any other id loads the default revision.
    """
    with _MODEL_LOCK:
        model: Any = _MODEL_CACHE.get(model_name)
        if model is None:
            from sentence_transformers import SentenceTransformer  # lazy — avoids torch at import

            model = SentenceTransformer(model_name, revision=PINNED_REVISIONS.get(model_name))
            _MODEL_CACHE[model_name] = model
        return model


class EmbeddingClient:
    """Async wrapper around a sentence-transformers model with an in-process cache.

    The model object is injected, keeping this class decoupled from any
    specific embedding library. Use from_model_name() for normal application
    use; inject a fake model in tests to avoid loading torch.

    Cache semantics: each unique text string is embedded at most once per
    EmbeddingClient lifetime. Calling embed("same text") twice costs one
    model inference call, not two. The model itself is shared process-wide
    (see from_model_name); the text cache is per client.
    """

    def __init__(self, model: Any) -> None:
        self._model = model
        self._cache: dict[str, list[float]] = {}

    @classmethod
    def from_model_name(cls, model_name: str = "all-MiniLM-L6-v2") -> EmbeddingClient:
        """Return a ready client backed by the process-wide model for *model_name*.

        The model is loaded synchronously on the first call for a given name
        (may download from HuggingFace Hub on first use) and reused by every
        later call, thread-safely. Calls to embed/embed_batch are async.

        Args:
            model_name: sentence-transformers model identifier. Callers pass
                ``settings.sentinel_embedding_model``; the default is
                all-MiniLM-L6-v2 (384 dims, ~22MB, zero cost, runs on CPU).
        """
        return cls(_load_model(model_name))

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string.

        Cached — calling with the same text is free after the first call.

        Args:
            text: The input text to embed.

        Returns:
            Embedding vector as list[float] with length equal to model dims.
        """
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts, using the cache for any already-seen strings.

        Deduplicates before calling the model: if the same text appears
        multiple times in texts, or has been seen in a prior call, the model
        is only invoked once for that string.

        Args:
            texts: Input strings to embed. May contain duplicates.

        Returns:
            List of embedding vectors in the same order as texts. Length
            always equals len(texts).
        """
        if not texts:
            return []

        uncached = [t for t in texts if t not in self._cache]

        if uncached:
            # Deduplicate while preserving first-seen order
            unique_uncached = list(dict.fromkeys(uncached))
            # encode() is sync and CPU-bound: run it off the event loop. to_thread (not
            # run_in_executor) copies contextvars, so the active trace span survives.
            raw: Any = await asyncio.to_thread(
                lambda: self._model.encode(
                    unique_uncached,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
            )
            for text, vec in zip(unique_uncached, raw):
                self._cache[text] = vec.tolist()

        return [self._cache[t] for t in texts]

    @property
    def cache_size(self) -> int:
        """Number of distinct texts currently cached."""
        return len(self._cache)

    def clear_cache(self) -> None:
        """Evict all cached embeddings. The next embed call will re-run inference."""
        self._cache.clear()
