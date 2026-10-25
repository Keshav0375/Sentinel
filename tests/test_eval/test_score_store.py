"""Tests for eval/score_store.py — the runner's incidents.eval_score writer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sentinel.config import Settings
from sentinel.eval.score_store import close_score_store, open_score_store
from sentinel.exceptions import SentinelMemoryError
from sentinel.memory.episodic import EpisodicMemory


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


async def test_no_database_returns_none() -> None:
    with (
        patch("sentinel.eval.score_store.init_pool_from_settings", new_callable=AsyncMock) as init,
        patch("sentinel.eval.score_store.logger") as log,
    ):
        init.return_value = None
        assert await open_score_store(_settings()) is None
    assert log.warning.call_args.args[0] == "eval_score_store_unavailable"


async def test_pool_failure_returns_none() -> None:
    with (
        patch(
            "sentinel.eval.score_store.init_pool_from_settings",
            new_callable=AsyncMock,
            side_effect=SentinelMemoryError("down"),
        ),
        patch("sentinel.eval.score_store.logger") as log,
    ):
        assert await open_score_store(_settings()) is None
    assert log.warning.call_args.args[0] == "eval_score_store_unavailable"
    assert log.warning.call_args.kwargs["error"] == "down"


async def test_pool_yields_episodic_memory_without_loading_a_model() -> None:
    with (
        patch(
            "sentinel.eval.score_store.init_pool_from_settings",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ),
        patch("sentinel.memory.embeddings._load_model") as load_model,
    ):
        store = await open_score_store(_settings())
    assert isinstance(store, EpisodicMemory)
    load_model.assert_not_called()


async def test_close_score_store_closes_the_pool() -> None:
    with patch("sentinel.eval.score_store.close_pool", new_callable=AsyncMock) as close:
        await close_score_store()
    close.assert_awaited_once()
