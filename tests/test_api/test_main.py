"""Tests for sentinel.main — create_app() and make_pipeline_fn()."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from sentinel.config import Settings
from sentinel.main import create_app, make_pipeline_fn
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.incident import IncidentStatus

# ── Helpers ───────────────────────────────────────────────────────────────────

_ORCH_PATCH = "sentinel.main._build_orchestrator_for_scenario"


def _fake_settings() -> Settings:
    return Settings(groq_api_key="gsk_test", _env_file=None)


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
    return make_pipeline_fn(
        _fake_settings, MagicMock(), MagicMock(), stm, **kwargs
    )


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
        patch("sentinel.main.Runner.run", new_callable=AsyncMock, return_value=mock_result),
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
            "sentinel.main.Runner.run",
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
            "sentinel.main.Runner.run",
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
        patch("sentinel.main.Runner.run", side_effect=clear_during_pipeline),
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
        patch("sentinel.main.Runner.run", side_effect=capture_input),
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
        patch("sentinel.main.Runner.run", side_effect=capture_kwargs),
    ):
        fn = _make_fn(stm, max_turns=7)
        await fn("inc-test0001", _make_alert())

    assert captured_kwargs[0]["max_turns"] == 7


@pytest.mark.asyncio
async def test_pipeline_fn_default_max_turns() -> None:
    """Default max_turns matches the documented tool-call cap (15)."""
    stm = _populated_stm()
    captured: list[dict[str, Any]] = []

    async def capture_kwargs(agent: Any, input_text: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs)
        return MagicMock()

    with (
        patch(_ORCH_PATCH, return_value=MagicMock()),
        patch("sentinel.main.Runner.run", side_effect=capture_kwargs),
    ):
        fn = _make_fn(stm)
        await fn("inc-test0001", _make_alert())

    assert captured[0]["max_turns"] == 15
