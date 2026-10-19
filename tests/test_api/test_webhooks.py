"""Tests for sentinel.api.webhooks — POST /webhooks/incident (§3.1).

Requests go through an httpx ASGI transport (not TestClient) so the pipeline tasks run on
the test's own event loop, next to the asyncpg pool. The tests marked (DB) run against a
throwaway database migrated to 001 (the §5.3 schema) on TEST_DATABASE_URL: they skip
locally without it and fail in CI, the same pattern as tests/test_api/test_incidents.py.
The real-pipeline tests run the orchestrator on the fake LLM (no key, no network).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from structlog.testing import capture_logs

from sentinel.agents.orchestrator import REFLEXION_MAX_LOOPS
from sentinel.api.webhooks import (
    ACCEPTED_MESSAGE,
    DUPLICATE_MESSAGE,
    CorrelationDeduplicator,
    PipelineFn,
    make_incident_router,
)
from sentinel.config import Settings
from sentinel.infra.database import create_pool
from sentinel.main import make_pipeline_fn
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.analysis import AnalysisResult
from sentinel.models.deploy import CorrelatedDeploy
from sentinel.models.incident import IncidentStatus, Severity, TriageResult
from sentinel.models.pipeline import PipelineInput
from sentinel.models.reflexion import SuspectDeploy
from sentinel.providers import (
    REFLEXION_AGENT,
    REVERIFICATION_AGENT,
    FakeScript,
    FakeToolCall,
    FakeTurn,
    fake_script,
    reflexion_turn,
    reverification_turn,
)

SERVICE = "sentinel-watchtower"
SHA = "a1b2c3d"
FULL_SHA = "a1b2c3d4e5f6"
PR = 42
DEPLOYED_AT = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

# ── Database fixtures ─────────────────────────────────────────────────────────


def _with_database(dsn: str, name: str) -> str:
    return urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))


def _execute(dsn: str, sql: str) -> None:
    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_go())


@pytest.fixture(scope="module")
def _module_db() -> Iterator[dict[str, str]]:
    """Holder for this module's throwaway database; dropped when the module finishes."""
    holder: dict[str, str] = {}
    yield holder
    if holder:
        _execute(holder["server"], f'DROP DATABASE IF EXISTS "{holder["name"]}" WITH (FORCE)')


@pytest.fixture
def webhooks_dsn(
    pg_dsn: str, _module_db: dict[str, str], alembic_config: Callable[..., Config]
) -> str:
    """A database at migration 001 with ``deployments`` and ``incidents`` emptied."""
    if not _module_db:
        name = f"sentinel_webhooks_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"]), "001")
    _execute(_module_db["dsn"], "DELETE FROM deployments; DELETE FROM incidents")
    return _module_db["dsn"]


@pytest.fixture
async def pool(webhooks_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(webhooks_dsn, min_size=1, max_size=4)
    try:
        yield p
    finally:
        await p.close()


class _Embedder:
    """A 384-dim embedding client stand-in: one fixed non-zero vector, no model load."""

    async def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 383


@pytest.fixture
def episodic(pool: asyncpg.Pool) -> EpisodicMemory:
    return EpisodicMemory(pool, _Embedder())  # type: ignore[arg-type]


# ── Helpers ───────────────────────────────────────────────────────────────────


class _Stub:
    """A pipeline_fn that records the requests it gets and does nothing else."""

    def __init__(self) -> None:
        self.requests: list[PipelineInput] = []

    async def __call__(self, request: PipelineInput) -> None:
        self.requests.append(request)


def _app(
    stm: ShortTermMemory,
    episodic: EpisodicMemory,
    pipeline_fn: PipelineFn | None,
    tasks: set[asyncio.Task[None]],
    dedup: CorrelationDeduplicator | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        make_incident_router(stm, episodic, pipeline_fn, deduplicator=dedup, tasks=tasks)
    )
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _no_db() -> EpisodicMemory:
    return EpisodicMemory(None, MagicMock())


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "signal_type": "runtime_error",
        "correlation_id": str(uuid.uuid4()),
        "title": "Synthetic check failed on sentinel-watchtower",
        "source": "datadog",
        "alert_id": "alert-1",
        "dd_event_id": "evt-1",
        "severity": "critical",
        "tags": {"service": SERVICE, "deploy_status": "failed", "failed_stage": "verify"},
        "context": {},
        "timestamp": "2026-10-15T12:00:00Z",
        "raw_payload": {"k": "v"},
    }
    return body | overrides


async def _drain(tasks: set[asyncio.Task[None]]) -> None:
    pending = list(tasks)
    if pending:
        await asyncio.gather(*pending)


def _real_pipeline(
    pool: asyncpg.Pool, episodic: EpisodicMemory, stm: ShortTermMemory
) -> PipelineFn:
    settings = Settings(_env_file=None, sentinel_fake_llm=True)  # type: ignore[call-arg]
    return make_pipeline_fn(lambda: settings, SemanticMemory(pool), episodic, stm, pool=pool)


async def _incident_row(pool: asyncpg.Pool, incident_id: str) -> asyncpg.Record:
    row = await pool.fetchrow("SELECT * FROM incidents WHERE id = $1", uuid.UUID(incident_id))
    assert row is not None
    return row


async def _deploy_row(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        INSERT INTO deployments (service, pr_number, commit_sha, deploy_status, deployed_at,
                                 files_changed, metadata)
        VALUES ($1, $2, $3, 'failed', $4, '["app/main.py"]'::jsonb,
                jsonb_build_object('failed_stage', 'verify', 'version', 'pr-42-a1b2c3d'))
        """,
        SERVICE,
        PR,
        FULL_SHA,
        DEPLOYED_AT,
    )


def _happy(script: FakeScript, incident_id: str) -> None:
    """A scripted happy path: a high-confidence critique of PR 42, a PASSing reverification."""
    suspect = CorrelatedDeploy(
        pr_number=PR, commit_sha=SHA, deployed_at=DEPLOYED_AT, deploy_status="failed"
    )
    spec = {
        "incident_id": incident_id,
        "root_cause": "pr-42 changed GET / to return HTML",
        "evidence_summary": "every check failed after pr-42 deployed",
        "confidence": 0.9,
        "justification": "the previous version passed the same checks",
        "target_deploy": {
            "pr_number": PR,
            "commit_sha": SHA,
            "deployed_at": DEPLOYED_AT.isoformat(),
        },
    }
    script.on(
        "triage",
        FakeTurn(
            output=TriageResult(
                severity=Severity.P2,
                affected_service=SERVICE,
                is_duplicate=False,
                recommended_action="check the last deploy",
            )
        ),
    )
    script.on(
        "analysis",
        FakeTurn(tool_calls=[FakeToolCall("fetch_logs", {"request": {"service": SERVICE}})]),
        FakeTurn(
            output=AnalysisResult(
                hypothesis="GET / returns HTML since pr-42",
                evidence=["fetch_logs: every check failed after pr-42"],
                suspect_deploy=suspect,
            )
        ),
    )
    script.on(
        REFLEXION_AGENT,
        reflexion_turn(
            0.85,
            deploy=SuspectDeploy(commit_sha=SHA, pr_number=PR),
            supporting_evidence=["checks fail since pr-42"],
        ),
    )
    script.on(
        "resolution",
        FakeTurn(tool_calls=[FakeToolCall("prepare_rollback_spec", {"request": spec})]),
        FakeTurn(output="Drafted the rollback of PR #42."),
    )
    script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))


def _timeline(row: asyncpg.Record) -> list[dict[str, Any]]:
    return json.loads(row["trajectory"])["timeline"]["timeline"]


# ── Request and response ──────────────────────────────────────────────────────


async def test_accepted_echoes_ids_and_header() -> None:
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    body = _payload()
    async with _client(_app(stm, _no_db(), stub, tasks)) as client:
        r = await client.post("/webhooks/incident", json=body)
        await _drain(tasks)

    assert r.status_code == 202
    ack = r.json()
    assert ack["status"] == "accepted" and ack["message"] == ACCEPTED_MESSAGE
    assert ack["correlation_id"] == body["correlation_id"]
    assert str(uuid.UUID(ack["incident_id"])) == ack["incident_id"]
    assert r.headers["X-Correlation-ID"] == body["correlation_id"]
    assert [q.incident_id for q in stub.requests] == [ack["incident_id"]]
    assert stub.requests[0].correlation_id == body["correlation_id"]
    context = stm.get_context(ack["incident_id"])
    assert context["alert"].service == SERVICE
    assert context["status"] is IncidentStatus.TRIAGE


@pytest.mark.parametrize("field", ["signal_type", "correlation_id", "title"])
async def test_missing_required_field_is_422(field: str) -> None:
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    body = _payload()
    del body[field]
    async with _client(_app(stm, _no_db(), stub, tasks)) as client:
        r = await client.post("/webhooks/incident", json=body)

    assert r.status_code == 422
    assert stub.requests == [] and stm.active_count == 0 and not tasks


@pytest.mark.parametrize(
    "override",
    [
        {"signal_type": "deploy-failure"},
        {"correlation_id": "corr-1"},
        {"title": "  "},
        {"tags": ["service:x"]},
    ],
)
async def test_invalid_fields_are_422(override: dict[str, Any]) -> None:
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    async with _client(_app(stm, _no_db(), stub, tasks)) as client:
        r = await client.post("/webhooks/incident", json=_payload(**override))

    assert r.status_code == 422
    assert stub.requests == [] and stm.active_count == 0


async def test_optional_fields_default() -> None:
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    body = {
        "signal_type": "runtime_error",
        "correlation_id": str(uuid.uuid4()),
        "title": "something broke",
    }
    async with _client(_app(stm, _no_db(), stub, tasks)) as client:
        r = await client.post("/webhooks/incident", json=body)
        await _drain(tasks)

    assert r.status_code == 202
    alert = stub.requests[0].alert
    assert (alert.severity, alert.service, alert.alert_id) == ("unknown", "unknown", "unknown")


async def test_tags_as_datadog_string() -> None:
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    tags = (
        "service:sentinel-watchtower, deploy_status:failed,failed_stage:verify,"
        "version:pr-5-a3f9c2,env:dev,novalue"
    )
    async with _client(_app(stm, _no_db(), stub, tasks)) as client:
        r = await client.post("/webhooks/incident", json=_payload(tags=tags))
        await _drain(tasks)

    assert r.status_code == 202
    alert = stub.requests[0].alert
    assert alert.service == SERVICE
    assert alert.tags.deploy_status == "failed"
    assert alert.tags.failed_stage == "verify"
    assert alert.tags.version == "pr-5-a3f9c2"


async def test_tags_as_object() -> None:
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    tags = {"service": SERVICE, "version": "pr-5-a3f9c2", "env": "dev"}
    async with _client(_app(stm, _no_db(), stub, tasks)) as client:
        r = await client.post("/webhooks/incident", json=_payload(tags=tags))
        await _drain(tasks)

    assert r.status_code == 202
    alert = stub.requests[0].alert
    assert alert.service == SERVICE and alert.tags.version == "pr-5-a3f9c2"
    assert not hasattr(alert.tags, "env")


async def test_mismatched_header_is_logged_not_rejected() -> None:
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    body = _payload()
    async with _client(_app(stm, _no_db(), stub, tasks)) as client:
        with capture_logs() as logs:
            r = await client.post(
                "/webhooks/incident", json=body, headers={"X-Correlation-ID": str(uuid.uuid4())}
            )
            same = await client.post(
                "/webhooks/incident",
                json=_payload(),
                headers={"X-Correlation-ID": "not-a-uuid"},
            )
        await _drain(tasks)

    assert r.status_code == 202 and same.status_code == 202
    assert r.headers["X-Correlation-ID"] == body["correlation_id"]
    mismatches = [e for e in logs if e["event"] == "correlation_header_mismatch"]
    assert len(mismatches) == 2  # a different UUID, and a non-UUID


async def test_matching_header_is_not_logged() -> None:
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    body = _payload()
    async with _client(_app(stm, _no_db(), stub, tasks)) as client:
        with capture_logs() as logs:
            r = await client.post(
                "/webhooks/incident",
                json=body,
                headers={"X-Correlation-ID": body["correlation_id"].upper()},
            )
        await _drain(tasks)

    assert r.status_code == 202
    assert not [e for e in logs if e["event"] == "correlation_header_mismatch"]


async def test_no_pipeline_wired_still_accepts() -> None:
    stm, tasks = ShortTermMemory(), set[asyncio.Task[None]]()
    async with _client(_app(stm, _no_db(), None, tasks)) as client:
        with capture_logs() as logs:
            r = await client.post("/webhooks/incident", json=_payload())

    assert r.status_code == 202 and not tasks
    assert [e for e in logs if e["event"] == "pipeline_not_wired"]
    assert stm.active_count == 1


# ── Dedupe ────────────────────────────────────────────────────────────────────


async def test_duplicate_in_memory_returns_original() -> None:
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    body = _payload()
    async with _client(_app(stm, _no_db(), stub, tasks)) as client:
        first = await client.post("/webhooks/incident", json=body)
        second = await client.post("/webhooks/incident", json=body)
        await _drain(tasks)

    assert first.json()["status"] == "accepted"
    dup = second.json()
    assert second.status_code == 202 and dup["status"] == "duplicate"
    assert dup["message"] == DUPLICATE_MESSAGE
    assert dup["incident_id"] == first.json()["incident_id"]
    assert second.headers["X-Correlation-ID"] == body["correlation_id"]
    assert len(stub.requests) == 1 and stm.active_count == 1


async def test_duplicate_from_db_returns_stored_id(
    pool: asyncpg.Pool, episodic: EpisodicMemory
) -> None:
    stored_id, correlation_id = uuid.uuid4(), str(uuid.uuid4())
    await pool.execute(
        "INSERT INTO incidents (id, alert_id, severity, service, status, correlation_id)"
        " VALUES ($1, 'a', 'P2', 's', 'escalated', $2)",
        stored_id,
        uuid.UUID(correlation_id),
    )
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    # A fresh router: nothing in its cache, so the stored incident is what dedupes.
    async with _client(_app(stm, episodic, stub, tasks)) as client:
        r = await client.post("/webhooks/incident", json=_payload(correlation_id=correlation_id))
        again = await client.post(
            "/webhooks/incident", json=_payload(correlation_id=correlation_id)
        )

    assert r.status_code == 202
    assert r.json()["status"] == "duplicate" and r.json()["incident_id"] == str(stored_id)
    assert again.json()["incident_id"] == str(stored_id)  # now from the cache
    assert stub.requests == [] and stm.active_count == 0


async def test_db_unavailable_dedupe_still_accepts() -> None:
    stm, stub, tasks = ShortTermMemory(), _Stub(), set[asyncio.Task[None]]()
    async with _client(_app(stm, _no_db(), stub, tasks)) as client:
        with capture_logs() as logs:
            r = await client.post("/webhooks/incident", json=_payload())
        await _drain(tasks)

    assert r.status_code == 202 and r.json()["status"] == "accepted"
    assert [e for e in logs if e["event"] == "dedupe_lookup_unavailable"]
    assert len(stub.requests) == 1


def test_deduplicator_ttl_expires() -> None:
    now = [1000.0]
    dedup = CorrelationDeduplicator(clock=lambda: now[0])
    assert CorrelationDeduplicator.TTL_SECONDS == 86400
    dedup.record("c1", "i1")

    now[0] += 86400 - 1
    assert dedup.check("c1") == "i1"
    now[0] += 2
    assert dedup.check("c1") is None
    assert dedup.size == 0


def test_deduplicator_record_evicts_expired() -> None:
    now = [0.0]
    dedup = CorrelationDeduplicator(clock=lambda: now[0])
    dedup.record("old", "i1")
    now[0] = 86400.0
    dedup.record("new", "i2")

    assert dedup.size == 1
    assert dedup.check("old") is None and dedup.check("new") == "i2"
    assert dedup.evict_expired() == 0


# ── One task per incident ─────────────────────────────────────────────────────


async def test_two_concurrent_posts_run_concurrently() -> None:
    barrier = asyncio.Barrier(2)

    async def pipeline(request: PipelineInput) -> None:
        async with asyncio.timeout(2):
            await barrier.wait()  # only returns when both incidents run at once

    stm, tasks = ShortTermMemory(), set[asyncio.Task[None]]()
    async with _client(_app(stm, _no_db(), pipeline, tasks)) as client:
        first, second = await asyncio.gather(
            client.post("/webhooks/incident", json=_payload()),
            client.post("/webhooks/incident", json=_payload()),
        )
        await _drain(tasks)  # raises TimeoutError if the two did not overlap

    assert first.status_code == second.status_code == 202
    assert first.json()["incident_id"] != second.json()["incident_id"]
    await asyncio.sleep(0)  # the done callbacks run a tick after the tasks finish
    assert not tasks  # finished tasks are released
    assert stm.active_count == 2


async def test_task_is_named_for_its_incident() -> None:
    seen: list[str] = []

    async def pipeline(request: PipelineInput) -> None:
        task = asyncio.current_task()
        assert task is not None
        seen.append(task.get_name())

    stm, tasks = ShortTermMemory(), set[asyncio.Task[None]]()
    async with _client(_app(stm, _no_db(), pipeline, tasks)) as client:
        r = await client.post("/webhooks/incident", json=_payload())
        await _drain(tasks)

    assert seen == [f"incident-{r.json()['incident_id']}"]


async def test_two_concurrent_incidents_are_isolated(
    pool: asyncpg.Pool, episodic: EpisodicMemory
) -> None:
    stm, tasks = ShortTermMemory(), set[asyncio.Task[None]]()
    app = _app(stm, episodic, _real_pipeline(pool, episodic, stm), tasks)
    payloads = [
        _payload(tags={"service": "svc-a"}),
        _payload(tags={"service": "svc-b"}),
    ]
    async with _client(app) as client:
        with fake_script():
            responses = await asyncio.gather(
                *(client.post("/webhooks/incident", json=p) for p in payloads)
            )
            await _drain(tasks)

    assert [r.status_code for r in responses] == [202, 202]
    for body, response in zip(payloads, responses, strict=True):
        incident_id = response.json()["incident_id"]
        row = await _incident_row(pool, incident_id)
        assert row["correlation_id"] == uuid.UUID(body["correlation_id"])
        assert row["service"] == body["tags"]["service"]
        assert row["status"] == "escalated"
        context = stm.get_context(incident_id)
        assert context["status"] is IncidentStatus.ESCALATED
        assert context["result"]["incident_id"] == incident_id
        assert context["alert"].service == body["tags"]["service"]
    ids = {r.json()["incident_id"] for r in responses}
    assert len(ids) == 2 and len(await pool.fetch("SELECT id FROM incidents")) == 2


# ── signal_type drives the branch, and the outcome is persisted ───────────────


async def test_runtime_error_takes_full_loop(pool: asyncpg.Pool, episodic: EpisodicMemory) -> None:
    stm, tasks = ShortTermMemory(), set[asyncio.Task[None]]()
    app = _app(stm, episodic, _real_pipeline(pool, episodic, stm), tasks)
    async with _client(app) as client:
        with fake_script():
            r = await client.post("/webhooks/incident", json=_payload())
            await _drain(tasks)

    row = await _incident_row(pool, r.json()["incident_id"])
    assert row["reflexion_loops"] == REFLEXION_MAX_LOOPS
    plan = next(
        e for e in _timeline(row) if e["agent_name"] == "orchestrator" and e["action"] == "plan"
    )
    assert plan["result_summary"].startswith("runtime_error: full loop")


async def test_deploy_failure_takes_fast_path(pool: asyncpg.Pool, episodic: EpisodicMemory) -> None:
    stm, tasks = ShortTermMemory(), set[asyncio.Task[None]]()
    app = _app(stm, episodic, _real_pipeline(pool, episodic, stm), tasks)
    async with _client(app) as client:
        with fake_script():
            r = await client.post("/webhooks/incident", json=_payload(signal_type="deploy_failure"))
            await _drain(tasks)

    row = await _incident_row(pool, r.json()["incident_id"])
    assert row["reflexion_loops"] == 0
    plan = next(
        e for e in _timeline(row) if e["agent_name"] == "orchestrator" and e["action"] == "plan"
    )
    assert plan["result_summary"].startswith("deploy_failure: fast path")


async def test_terminal_incident_stored(pool: asyncpg.Pool, episodic: EpisodicMemory) -> None:
    stm, tasks = ShortTermMemory(), set[asyncio.Task[None]]()
    app = _app(stm, episodic, _real_pipeline(pool, episodic, stm), tasks)
    body = _payload()
    async with _client(app) as client:
        with fake_script():
            r = await client.post("/webhooks/incident", json=body)
            await _drain(tasks)

    incident_id = r.json()["incident_id"]
    row = await episodic.get_incident_row(incident_id)
    assert row is not None
    assert row.status == "escalated" and row.resolution_type == "escalated"
    assert row.correlation_id == body["correlation_id"]
    assert row.dd_event_id == body["dd_event_id"]
    assert row.alert_id == body["alert_id"] and row.service == SERVICE
    assert isinstance(row.trajectory, dict)
    assert "timeline" in row.trajectory and "pipeline_result" in row.trajectory


# ── Deployment backfill and the fast-path pin ─────────────────────────────────


async def test_rollback_proposed_backfills_deployment(
    pool: asyncpg.Pool, episodic: EpisodicMemory
) -> None:
    await _deploy_row(pool)
    stm, tasks = ShortTermMemory(), set[asyncio.Task[None]]()
    app = _app(stm, episodic, _real_pipeline(pool, episodic, stm), tasks)
    body = _payload(signal_type="deploy_failure", context={"pr_details": {"sha": FULL_SHA}})
    async with _client(app) as client:
        with fake_script() as script:
            # The resolution spec's incident_id may be any UUID: the orchestrator corrects it.
            _happy(script, str(uuid.uuid4()))
            r = await client.post("/webhooks/incident", json=body)
            await _drain(tasks)

    incident_id = r.json()["incident_id"]
    row = await _incident_row(pool, incident_id)
    assert row["status"] == "rollback_proposed"
    assert await pool.fetchval("SELECT incident_id FROM deployments") == uuid.UUID(incident_id)
    linked = await episodic.get_linked_deployment(incident_id)
    assert linked is not None and linked.commit_sha == FULL_SHA


async def test_escalation_does_not_backfill(pool: asyncpg.Pool, episodic: EpisodicMemory) -> None:
    await _deploy_row(pool)
    stm, tasks = ShortTermMemory(), set[asyncio.Task[None]]()
    app = _app(stm, episodic, _real_pipeline(pool, episodic, stm), tasks)
    async with _client(app) as client:
        with fake_script():
            r = await client.post("/webhooks/incident", json=_payload())
            await _drain(tasks)

    assert (await _incident_row(pool, r.json()["incident_id"]))["status"] == "escalated"
    assert await pool.fetchval("SELECT incident_id FROM deployments") is None


async def test_fast_path_pin_mismatch_escalates(
    pool: asyncpg.Pool, episodic: EpisodicMemory
) -> None:
    await _deploy_row(pool)
    stm, tasks = ShortTermMemory(), set[asyncio.Task[None]]()
    app = _app(stm, episodic, _real_pipeline(pool, episodic, stm), tasks)
    body = _payload(signal_type="deploy_failure", context={"pr_details": {"sha": "ffffffffffff"}})
    async with _client(app) as client:
        with fake_script() as script:
            _happy(script, str(uuid.uuid4()))
            r = await client.post("/webhooks/incident", json=body)
            await _drain(tasks)

    row = await _incident_row(pool, r.json()["incident_id"])
    assert row["status"] == "escalated"
    result = json.loads(row["trajectory"])["pipeline_result"]
    assert "context.pr_details.sha" in result["escalation"]["missing"][0]
    assert await pool.fetchval("SELECT incident_id FROM deployments") is None
