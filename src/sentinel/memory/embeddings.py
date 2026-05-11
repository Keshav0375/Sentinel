"""Embedding client — wraps sentence-transformers for local inference."""

from __future__ import annotations

import asyncio
from typing import Any


class EmbeddingClient:
    """Async wrapper around a sentence-transformers model with an in-process cache.

    The model object is injected, keeping this class decoupled from any
    specific embedding library. Use from_model_name() for normal application
    use; inject a fake model in tests to avoid loading torch.

    Cache semantics: each unique text string is embedded at most once per
    EmbeddingClient lifetime. Calling embed("same text") twice costs one
    model inference call, not two.
    """

    def __init__(self, model: Any) -> None:
        self._model = model
        self._cache: dict[str, list[float]] = {}

    @classmethod
    def from_model_name(cls, model_name: str = "all-MiniLM-L6-v2") -> EmbeddingClient:
        """Load a SentenceTransformer model by name and return a ready client.

        The model is loaded synchronously on first call (may download from
        HuggingFace Hub on first use). Subsequent calls to embed/embed_batch
        are async.

        Args:
            model_name: sentence-transformers model identifier. Defaults to
                all-MiniLM-L6-v2 (384 dims, ~22MB, zero cost, runs on CPU).
        """
        from sentence_transformers import SentenceTransformer  # lazy — avoids torch at import time

        return cls(SentenceTransformer(model_name))

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
