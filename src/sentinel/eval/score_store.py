"""The eval runner's ``incidents.eval_score`` writer.

The runner stores each judged score on its incident row through ``EpisodicMemory``. The pool
comes from ``DATABASE_URL`` only (no Entra password factory: that path needs the pod's
workload identity). No database is not an error: ``open_score_store`` returns None and the
runner scores without storing.
"""

from __future__ import annotations

from typing import Final

from sentinel.config import Settings
from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.database import close_pool, init_pool_from_settings
from sentinel.infra.logging import get_logger
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory

logger = get_logger("sentinel.eval.score_store")

__all__ = ["open_score_store", "close_score_store"]

# store_eval_score never embeds: no sentence-transformers model is loaded.
_NO_EMBEDDING_MODEL: Final = None


async def open_score_store(settings: Settings) -> EpisodicMemory | None:
    """An :class:`EpisodicMemory` over ``DATABASE_URL``, or None (logged) when there is none."""
    try:
        pool = await init_pool_from_settings(settings)
    except SentinelMemoryError as exc:
        logger.warning(
            "eval_score_store_unavailable", reason="database pool failed", error=str(exc)
        )
        return None
    if pool is None:
        logger.warning(
            "eval_score_store_unavailable", reason="no database configured (DATABASE_URL)"
        )
        return None
    return EpisodicMemory(pool, EmbeddingClient(_NO_EMBEDDING_MODEL))


async def close_score_store() -> None:
    """Close the pool open_score_store opened; a no-op when none is open."""
    await close_pool()
