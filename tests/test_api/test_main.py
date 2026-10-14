"""Tests for sentinel.main — create_app() and make_pipeline_fn()."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import FastAPI

from sentinel import main
from sentinel.config import Settings
from sentinel.infra import database
from sentinel.main import create_app, make_pipeline_fn
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.incident import IncidentStatus

# ── Helpers ───────────────────────────────────────────────────────────────────

_ORCH_PATCH = "sentinel.main._build_orchestrator"
# The pipeline runs the orchestrator through providers.router.call_with_fallback.
_RUN_PATCH = "agents.Runner.run"


def _fake_settings() -> Settings:
    return Settings(anthropic_api_key="sk-ant-test", openai_api_key="sk-test", _env_file=None)


def _make_alert(service: str = "api-gateway") -> AlertPayload:
    return AlertPayload(
        source=AlertSource.DATADOG,
        service=service,
        metric="error_rate",
        threshold=0.05,
        current_value=0.34,
        severity=AlertSeverity.CRITICAL,
    )


def _populated_stm(incident_id: str = "inc-test0001") -> ShortTermMemory:
    stm = ShortTermMemory()
    stm.create(incident_id, _make_alert())
    return stm


def _make_fn(stm: ShortTermMemory, **kwargs: Any) -> Any:
    """Build make_pipeline_fn with mock dependencies."""
    return make_pipeline_fn(_fake_settings, MagicMock(), MagicMock(), stm, **kwargs)


# ── create_app ────────────────────────────────────────────────────────────────


def test_create_app_returns_fastapi() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)


def test_create_app_title() -> None:
    app = create_app()
    assert app.title == "Sentinel"


def test_create_app_version() -> None:
    app = create_app()
    assert app.version == "0.1.0"


def test_create_app_has_lifespan() -> None:
    """App must have a lifespan handler set (not None)."""
    app = create_app()
    assert app.router.lifespan_context is not None


def test_module_level_app_is_fastapi() -> None:
    """The module-level ``app`` singleton must be importable without error."""
    from sentinel.main import app

    assert isinstance(app, FastAPI)


# ── lifespan: the Postgres pool never outlives a failed startup ──────────────


@pytest.fixture
def lifespan_env(pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the real lifespan at the test database and a throwaway SQLite file.

    Global side effects of a full startup (SDK default client, trace processors) are
    stubbed so they cannot leak into other tests.
    """
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DB_POOL_MIN", "1")
    monkeypatch.setenv("SENTINEL_DB_POOL_MAX", "2")
    monkeypatch.setattr(main, "apply_sdk_defaults", MagicMock())
    monkeypatch.setattr(main, "add_trace_processor", MagicMock())
    return pg_dsn


async def test_lifespan_startup_failure_closes_pool(
    lifespan_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[asyncpg.Pool | None] = []
    real_init = main.init_pool_from_settings

    async def _spy_init(settings: Settings) -> asyncpg.Pool | None:
        pool = await real_init(settings)
        opened.append(pool)
        return pool

    monkeypatch.setattr(main, "init_pool_from_settings", _spy_init)
    failing = MagicMock()
    failing.from_model_name.side_effect = RuntimeError("model load failed")
    monkeypatch.setattr(main, "EmbeddingClient", failing)

    # Startup fails after the pool is open.
    with pytest.raises(RuntimeError, match="model load failed"):
        async with main.lifespan(FastAPI()):
            pytest.fail("startup should not have completed")

    assert len(opened) == 1 and opened[0] is not None
    assert opened[0].is_closing()
    assert database._pool is None  # pyright: ignore[reportPrivateUsage]

    # The same process can start again: a fresh pool, usable, closed at shutdown.
    monkeypatch.setattr(main, "EmbeddingClient", MagicMock())
    async with main.lifespan(FastAPI()):
        pool = database.get_pool()
        assert pool is not opened[0]
        async with database.acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1
    assert pool.is_closing()
    assert database._pool is None  # pyright: ignore[reportPrivateUsage]


# ── make_pipeline_fn ──────────────────────────────────────────────────────────


def test_make_pipeline_fn_returns_callable() -> None:
    stm = ShortTermMemory()
    fn = _make_fn(stm)
    assert callable(fn)


@pytest.mark.asyncio
async def test_pipeline_fn_marks_incident_resolved_on_success() -> None:
    stm = _populated_stm()
    mock_result = MagicMock()

    with (
        patch(_ORCH_PATCH, return_value=MagicMock()),
        patch(_RUN_PATCH, new_callable=AsyncMock, return_value=mock_result),
    ):
        fn = _make_fn(stm)
        await fn("inc-test0001", _make_alert())

    assert stm.get_context("inc-test0001")["status"] == IncidentStatus.RESOLVED


@pytest.mark.asyncio
async def test_pipeline_fn_marks_incident_escalated_on_exception() -> None:
    stm = _populated_stm()

    with (
        patch(_ORCH_PATCH, return_value=MagicMock()),
        patch(
            _RUN_PATCH,
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM timeout"),
        ),
    ):
        fn = _make_fn(stm)
        await fn("inc-test0001", _make_alert())

    assert stm.get_context("inc-test0001")["status"] == IncidentStatus.ESCALATED


@pytest.mark.asyncio
async def test_pipeline_fn_does_not_raise_on_exception() -> None:
    """Exceptions inside the pipeline must be caught — never propagate to caller."""
    stm = _populated_stm()

    with (
        patch(_ORCH_PATCH, return_value=MagicMock()),
        patch(
            _RUN_PATCH,
            new_callable=AsyncMock,
            side_effect=Exception("catastrophic failure"),
        ),
    ):
        fn = _make_fn(stm)
        await fn("inc-test0001", _make_alert())


@pytest.mark.asyncio
async def test_pipeline_fn_handles_cleared_incident_gracefully() -> None:
    """If the incident is cleared from STM mid-pipeline, no KeyError."""
    stm = ShortTermMemory()
    stm.create("inc-cleared01", _make_alert())
    mock_result = MagicMock()

    async def clear_during_pipeline(*args: Any, **kwargs: Any) -> MagicMock:
        stm.clear("inc-cleared01")
        return mock_result

    with (
        patch(_ORCH_PATCH, return_value=MagicMock()),
        patch(_RUN_PATCH, side_effect=clear_during_pipeline),
    ):
        fn = _make_fn(stm)
        await fn("inc-cleared01", _make_alert())


@pytest.mark.asyncio
async def test_pipeline_fn_passes_incident_id_in_input() -> None:
    """The input string passed to Runner.run must contain the incident_id."""
    stm = _populated_stm("inc-idcheck0")
    captured: list[str] = []

    async def capture_input(agent: Any, input_text: str, **kwargs: Any) -> MagicMock:
        captured.append(input_text)
        return MagicMock()

    with (
        patch(_ORCH_PATCH, return_value=MagicMock()),
        patch(_RUN_PATCH, side_effect=capture_input),
    ):
        fn = _make_fn(stm)
        await fn("inc-idcheck0", _make_alert())

    assert len(captured) == 1
    assert "inc-idcheck0" in captured[0]


@pytest.mark.asyncio
async def test_pipeline_fn_respects_max_turns() -> None:
    """max_turns is passed through to Runner.run as a kwarg."""
    stm = _populated_stm()
    captured_kwargs: list[dict[str, Any]] = []

    async def capture_kwargs(agent: Any, input_text: str, **kwargs: Any) -> MagicMock:
        captured_kwargs.append(kwargs)
        return MagicMock()

    with (
        patch(_ORCH_PATCH, return_value=MagicMock()),
        patch(_RUN_PATCH, side_effect=capture_kwargs),
    ):
        fn = _make_fn(stm, max_turns=7)
        await fn("inc-test0001", _make_alert())

    assert captured_kwargs[0]["max_turns"] == 7


@pytest.mark.asyncio
async def test_pipeline_fn_default_max_turns() -> None:
    """Default max_turns matches the documented tool-call cap (20, §4.6)."""
    stm = _populated_stm()
    captured: list[dict[str, Any]] = []

    async def capture_kwargs(agent: Any, input_text: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs)
        return MagicMock()

    with (
        patch(_ORCH_PATCH, return_value=MagicMock()),
        patch(_RUN_PATCH, side_effect=capture_kwargs),
    ):
        fn = _make_fn(stm)
        await fn("inc-test0001", _make_alert())

    assert captured[0]["max_turns"] == 20


@pytest.mark.asyncio
async def test_pipeline_fn_escalates_when_provider_key_missing() -> None:
    """Keys are optional at boot: a missing one fails the incident at use, not the task."""
    stm = _populated_stm()

    def keyless_settings() -> Settings:
        return Settings(_env_file=None)  # type: ignore[call-arg]

    with patch(_RUN_PATCH, new_callable=AsyncMock) as run:
        fn = make_pipeline_fn(keyless_settings, MagicMock(), MagicMock(), stm)
        await fn("inc-test0001", _make_alert())

    run.assert_not_awaited()
    assert stm.get_context("inc-test0001")["status"] == IncidentStatus.ESCALATED
