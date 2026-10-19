"""Tests for sentinel.main — create_app() and make_pipeline_fn()."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import FastAPI

from sentinel import main
from sentinel.agents.orchestrator import PipelineDeps
from sentinel.config import Settings
from sentinel.infra import database
from sentinel.infra.event_bus import EventBus, EventType
from sentinel.main import create_app, make_pipeline_fn
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource, IncidentAlert
from sentinel.models.incident import IncidentStatus
from sentinel.models.pipeline import PipelineInput, PipelineResult
from sentinel.models.tools import EscalationContext, RollbackSpec, TargetDeploy

# ── Helpers ───────────────────────────────────────────────────────────────────


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


def _make_incident_alert(service: str = "api-gateway") -> IncidentAlert:
    return IncidentAlert(title="error_rate", severity="critical", service=service)


def _populated_stm(incident_id: str = "inc-test0001") -> ShortTermMemory:
    stm = ShortTermMemory()
    stm.create(incident_id, _make_incident_alert())
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
    monkeypatch.setattr(main, "install_trace_processors", MagicMock())
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

_RUN_PATCH = "sentinel.main.run_pipeline"
_INCIDENT = "4f7c2a5e-8f1d-4f0e-9c4b-2b8e6a1d3c70"


def _request(incident_id: str = _INCIDENT) -> PipelineInput:
    return PipelineInput.from_alert(incident_id, _make_alert())


def _result(status: str = "rollback_proposed", incident_id: str = _INCIDENT) -> PipelineResult:
    common: dict[str, Any] = {
        "incident_id": incident_id,
        "root_cause": "pr-42 broke GET /",
        "confidence": 0.9,
        "reflexion_loops": 1,
        "tool_calls_used": 4,
        "tokens_used": 1234,
        "models_used": ["anthropic/claude-haiku-4-5"],
    }
    if status == "rollback_proposed":
        spec = RollbackSpec(
            incident_id=incident_id,
            root_cause="pr-42 broke GET /",
            evidence_summary="checks fail since pr-42",
            confidence=0.9,
            justification="the previous version passed",
            target_deploy=TargetDeploy(
                pr_number=42, commit_sha="a1b2c3d", deployed_at=datetime(2026, 10, 15, tzinfo=UTC)
            ),
        )
        return PipelineResult(
            status="rollback_proposed", resolution_type="rollback", rollback_spec=spec, **common
        )
    escalation = EscalationContext(hypothesis="unclear", evidence=[], confidence=0.3, missing=["x"])
    return PipelineResult(
        status="escalated", resolution_type="escalated", escalation=escalation, **common
    )


def test_make_pipeline_fn_returns_callable() -> None:
    stm = ShortTermMemory()
    fn = _make_fn(stm)
    assert callable(fn)


@pytest.mark.asyncio
async def test_pipeline_fn_marks_rollback_proposed_and_stores_the_result() -> None:
    stm = _populated_stm(_INCIDENT)
    with patch(_RUN_PATCH, new_callable=AsyncMock, return_value=_result()) as run:
        fn = _make_fn(stm)
        await fn(_request())

    context = stm.get_context(_INCIDENT)
    assert context["status"] == IncidentStatus.ROLLBACK_PROPOSED
    assert context["result"]["resolution_type"] == "rollback"
    assert context["result"]["rollback_spec"]["target_deploy"]["commit_sha"] == "a1b2c3d"
    request, deps = run.await_args.args
    assert request.incident_id == _INCIDENT
    assert isinstance(deps, PipelineDeps) and deps.pool is None


@pytest.mark.asyncio
async def test_get_incident_returns_the_9_3_outputs_of_an_unstored_result() -> None:
    """target_deploy, target_sha and evidence_summary of an incident not yet stored are
    returned by GET /incidents/{id} under trajectory.pipeline_result, where the §9.3 job
    reads them (stored rows keep them there too)."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from sentinel.api.incidents import make_incidents_router  # noqa: PLC0415

    stm = _populated_stm(_INCIDENT)
    with patch(_RUN_PATCH, new_callable=AsyncMock, return_value=_result()):
        await _make_fn(stm)(_request())
    episodic = MagicMock(spec=EpisodicMemory)
    episodic.get_incident_row = AsyncMock(return_value=None)
    app = FastAPI()
    app.include_router(make_incidents_router(stm, episodic))
    with TestClient(app) as client:
        body = client.get(f"/incidents/{_INCIDENT}").json()

    assert body["status"] == "in_progress"
    pipeline_result = body["trajectory"]["pipeline_result"]
    assert pipeline_result["target_deploy"] == "a1b2c3d"
    assert pipeline_result["target_sha"] == "a1b2c3d"
    assert pipeline_result["evidence_summary"] == "checks fail since pr-42"
    assert pipeline_result["resolution_type"] == "rollback"


@pytest.mark.asyncio
async def test_pipeline_fn_marks_an_escalated_result_escalated() -> None:
    stm = _populated_stm(_INCIDENT)
    with patch(_RUN_PATCH, new_callable=AsyncMock, return_value=_result("escalated")):
        await _make_fn(stm)(_request())
    assert stm.get_context(_INCIDENT)["status"] == IncidentStatus.ESCALATED


@pytest.mark.asyncio
async def test_pipeline_fn_escalates_and_does_not_raise_on_an_exception() -> None:
    """A failure outside run_pipeline (which never raises) still ends escalated."""
    stm = _populated_stm(_INCIDENT)

    def broken_settings() -> Settings:
        raise RuntimeError("settings unavailable")

    fn = make_pipeline_fn(broken_settings, MagicMock(), MagicMock(), stm)
    await fn(_request())
    context = stm.get_context(_INCIDENT)
    assert context["status"] == IncidentStatus.ESCALATED and context["result"] is None


@pytest.mark.asyncio
async def test_pipeline_fn_handles_cleared_incident_gracefully() -> None:
    """If the incident is cleared from STM mid-pipeline, no KeyError."""
    stm = _populated_stm(_INCIDENT)

    async def clear_during_pipeline(*args: Any, **kwargs: Any) -> PipelineResult:
        stm.clear(_INCIDENT)
        return _result()

    with patch(_RUN_PATCH, side_effect=clear_during_pipeline):
        await _make_fn(stm)(_request())
    assert stm.active_count == 0


@pytest.mark.asyncio
async def test_pipeline_fn_publishes_the_terminal_status_and_closes_the_stream() -> None:
    stm = _populated_stm(_INCIDENT)
    bus = EventBus()
    queue = await bus.subscribe(_INCIDENT)
    with patch(_RUN_PATCH, new_callable=AsyncMock, return_value=_result()):
        await _make_fn(stm, event_bus=bus)(_request())

    event = queue.get_nowait()
    assert event is not None and event.type is EventType.INCIDENT_COMPLETED
    assert event.data == {"status": "rollback_proposed"}
    assert queue.get_nowait() is None  # end of stream
    assert bus.incident_subscriber_count(_INCIDENT) == 0


@pytest.mark.asyncio
async def test_pipeline_fn_escalates_when_provider_key_missing() -> None:
    """Keys are optional at boot: a missing one escalates the incident at use."""
    stm = _populated_stm(_INCIDENT)

    def keyless_settings() -> Settings:
        return Settings(_env_file=None)  # type: ignore[call-arg]

    fn = make_pipeline_fn(keyless_settings, MagicMock(), MagicMock(), stm)
    await fn(_request())

    context = stm.get_context(_INCIDENT)
    assert context["status"] == IncidentStatus.ESCALATED
    assert context["result"]["escalation"]["missing"][0] == (
        "pipeline error during triage: ProviderUnavailableError"
    )


@pytest.mark.asyncio
async def test_pipeline_runs_end_to_end_on_the_fake_llm_without_keys() -> None:
    """SENTINEL_FAKE_LLM=1: the real agents run on FakeModel, no key, no provider.

    Unscripted, every agent answers with a default the pipeline cannot act on, so the
    incident ends escalated — after the full reflexion loop, never pending.
    """
    stm = _populated_stm(_INCIDENT)

    def fake_settings() -> Settings:
        return Settings(_env_file=None, sentinel_fake_llm=True)  # type: ignore[call-arg]

    fn = make_pipeline_fn(
        fake_settings, SemanticMemory(None), EpisodicMemory(None, MagicMock()), stm
    )
    await fn(_request())

    context = stm.get_context(_INCIDENT)
    assert context["status"] == IncidentStatus.ESCALATED
    assert context["result"]["reflexion_loops"] == 2
    assert context["result"]["tool_calls_used"] == 0
