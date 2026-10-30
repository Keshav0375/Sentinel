"""Tests for sentinel.api.incidents — the GET /incidents* query routes.

The tests taking ``episodic`` / ``pool`` run against a throwaway database migrated to 001
(the §5.3 schema) on TEST_DATABASE_URL: they skip locally without it and fail in CI, the
same pattern as tests/test_memory/test_episodic.py. Requests go through an httpx ASGI
transport (not TestClient) because the asyncpg pool belongs to the test's event loop.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from sentinel.api.incidents import make_incidents_router, rollback_spec_of
from sentinel.infra.database import create_pool
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import IncidentAlert
from sentinel.models.incident import TimelineEntry
from sentinel.models.memory import IncidentRow
from sentinel.models.pipeline import PipelineResult
from sentinel.models.tools import EscalationContext, RollbackSpec, TargetDeploy

T0 = datetime(2026, 10, 1, tzinfo=UTC)

INCIDENT_COLUMNS = {
    "id",
    "alert_id",
    "dd_event_id",
    "correlation_id",
    "severity",
    "service",
    "status",
    "root_cause",
    "root_cause_confidence",
    "resolution",
    "resolution_type",
    "pr_number",
    "pr_url",
    "trajectory",
    "eval_score",
    "langfuse_trace_id",
    "reflexion_loops",
    "created_at",
    "resolved_at",
    "mttr_seconds",
}
DEPLOYMENT_COLUMNS = {
    "id",
    "service",
    "pr_number",
    "commit_sha",
    "author",
    "deploy_status",
    "gha_run_id",
    "dd_deploy_event_id",
    "files_changed",
    "incident_id",
    "deployed_at",
    "metadata",
}

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
def incidents_dsn(
    pg_dsn: str, _module_db: dict[str, str], alembic_config: Callable[..., Config]
) -> str:
    """A database at migration 001 with ``incidents`` and ``deployments`` emptied."""
    if not _module_db:
        name = f"sentinel_incidents_api_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"]), "001")
    _execute(_module_db["dsn"], "TRUNCATE incidents, deployments")
    return _module_db["dsn"]


@pytest.fixture
async def pool(incidents_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(incidents_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def episodic(pool: asyncpg.Pool) -> EpisodicMemory:
    return EpisodicMemory(pool, EmbeddingClient(MagicMock()))


# ── Helpers ───────────────────────────────────────────────────────────────────


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


async def _insert_incident(
    pool: asyncpg.Pool,
    *,
    service: str = "payment-service",
    status: str = "escalated",
    created_at: datetime,
    **columns: Any,
) -> str:
    """Insert one ``incidents`` row (all 20 columns but the embedding); return its id."""
    incident_id = str(columns.pop("id", None) or uuid.uuid4())
    values: dict[str, Any] = {
        "alert_id": f"alert-{uuid.uuid4().hex[:8]}",
        "severity": "P1",
        "dd_event_id": None,
        "correlation_id": None,
        "root_cause": None,
        "root_cause_confidence": None,
        "resolution": None,
        "resolution_type": None,
        "pr_number": None,
        "pr_url": None,
        "trajectory": None,
        "eval_score": None,
        "langfuse_trace_id": None,
        "reflexion_loops": None,
        "resolved_at": None,
        "mttr_seconds": None,
    } | columns
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO incidents
                (id, alert_id, dd_event_id, correlation_id, severity, service, status,
                 root_cause, root_cause_confidence, resolution, resolution_type, pr_number,
                 pr_url, trajectory, eval_score, langfuse_trace_id, reflexion_loops,
                 created_at, resolved_at, mttr_seconds)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb,
                    $15::jsonb, $16, $17, $18, $19, $20)
            """,
            uuid.UUID(incident_id),
            values["alert_id"],
            values["dd_event_id"],
            values["correlation_id"],
            values["severity"],
            service,
            status,
            values["root_cause"],
            values["root_cause_confidence"],
            values["resolution"],
            values["resolution_type"],
            values["pr_number"],
            values["pr_url"],
            _json(values["trajectory"]),
            _json(values["eval_score"]),
            values["langfuse_trace_id"],
            values["reflexion_loops"],
            created_at,
            values["resolved_at"],
            values["mttr_seconds"],
        )
    return incident_id


async def _insert_deployment(
    pool: asyncpg.Pool,
    *,
    service: str = "payment-service",
    commit_sha: str = "a1b2c3d4e5f6",
    incident_id: str | None,
    **columns: Any,
) -> str:
    """Insert one ``deployments`` row; return its id."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO deployments
                (service, pr_number, commit_sha, author, deploy_status, gha_run_id,
                 dd_deploy_event_id, files_changed, incident_id, deployed_at, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11::jsonb)
            RETURNING id
            """,
            service,
            columns.get("pr_number"),
            commit_sha,
            columns.get("author"),
            columns.get("deploy_status", "failed"),
            columns.get("gha_run_id"),
            columns.get("dd_deploy_event_id"),
            _json(columns.get("files_changed")),
            None if incident_id is None else uuid.UUID(incident_id),
            columns.get("deployed_at", datetime.now(UTC)),
            _json(columns.get("metadata")),
        )
    assert row is not None
    return str(row["id"])


def _alert(service: str = "payment-service") -> IncidentAlert:
    return IncidentAlert(
        alert_id="alert-live", title="db_connection_pool", severity="critical", service=service
    )


def _app(stm: ShortTermMemory, episodic: EpisodicMemory) -> FastAPI:
    app = FastAPI()
    app.include_router(make_incidents_router(stm, episodic))
    return app


async def _get(app: FastAPI, path: str, **params: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


# ── No database ───────────────────────────────────────────────────────────────

_HELD = str(uuid.uuid4())


@pytest.mark.parametrize(
    "path",
    [
        "/incidents",
        f"/incidents/{_HELD}",
        f"/incidents/{_HELD}/trajectory",
        f"/incidents/{_HELD}/deploy",
        f"/incidents/{_HELD}/rollback-spec",
    ],
)
async def test_every_route_is_503_without_a_database(path: str) -> None:
    """No pool is 503 on every route, even for an id short-term memory holds."""
    stm = ShortTermMemory()
    stm.create(_HELD, _alert())
    app = _app(stm, EpisodicMemory(None, EmbeddingClient(MagicMock())))

    response = await _get(app, path)

    assert response.status_code == 503
    assert "memory unavailable" in response.json()["detail"]


async def test_list_rejects_a_limit_below_one() -> None:
    app = _app(ShortTermMemory(), EpisodicMemory(None, EmbeddingClient(MagicMock())))
    assert (await _get(app, "/incidents", limit=0)).status_code == 422


async def test_list_rejects_a_limit_above_200() -> None:
    app = _app(ShortTermMemory(), EpisodicMemory(None, EmbeddingClient(MagicMock())))
    assert (await _get(app, "/incidents", limit=201)).status_code == 422


async def test_list_accepts_limit_200() -> None:
    # The in-progress listing also reads the in_progress placeholder rows: none here.
    episodic = MagicMock()
    episodic.stored_incident_ids = AsyncMock(return_value=set())
    episodic.list_incident_rows = AsyncMock(return_value=[])
    app = _app(ShortTermMemory(), episodic)
    response = await _get(app, "/incidents", status="in_progress", limit=200)
    assert response.status_code == 200
    assert response.json() == {"incidents": []}
    episodic.list_incident_rows.assert_awaited_once_with(
        status="in_progress", service=None, limit=200
    )


# ── GET /incidents ────────────────────────────────────────────────────────────


async def test_list_returns_stored_rows_newest_first_without_embedding(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    ids = [await _insert_incident(pool, created_at=T0 + timedelta(hours=h)) for h in (0, 1, 2)]

    response = await _get(_app(ShortTermMemory(), episodic), "/incidents")

    assert response.status_code == 200
    items = response.json()["incidents"]
    assert [item["id"] for item in items] == list(reversed(ids))
    for item in items:
        assert set(item) == INCIDENT_COLUMNS
        assert "embedding" not in item


async def test_list_filters_by_status_and_service(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    await _insert_incident(pool, service="svc-a", status="escalated", created_at=T0)
    await _insert_incident(pool, service="svc-a", status="rollback_proposed", created_at=T0)
    await _insert_incident(pool, service="svc-b", status="escalated", created_at=T0)
    app = _app(ShortTermMemory(), episodic)

    by_status = (await _get(app, "/incidents", status="escalated")).json()["incidents"]
    by_service = (await _get(app, "/incidents", service="svc-a")).json()["incidents"]
    both = (await _get(app, "/incidents", status="escalated", service="svc-a")).json()["incidents"]

    assert len(by_status) == 2
    assert len(by_service) == 2
    assert len(both) == 1
    assert both[0]["service"] == "svc-a" and both[0]["status"] == "escalated"


async def test_list_limit_keeps_the_newest(episodic: EpisodicMemory, pool: asyncpg.Pool) -> None:
    ids = [await _insert_incident(pool, created_at=T0 + timedelta(hours=h)) for h in (0, 1, 2)]

    response = await _get(_app(ShortTermMemory(), episodic), "/incidents", limit=2)

    assert [item["id"] for item in response.json()["incidents"]] == [ids[2], ids[1]]


async def test_list_puts_in_flight_incidents_first_as_in_progress(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    live = str(uuid.uuid4())
    stm = ShortTermMemory()
    stm.create(live, _alert("order-service"))
    stored = await _insert_incident(pool, created_at=T0)

    items = (await _get(_app(stm, episodic), "/incidents")).json()["incidents"]

    assert [item["id"] for item in items] == [live, stored]
    first = items[0]
    assert first["status"] == "in_progress"
    assert first["service"] == "order-service"
    assert first["alert_id"] == "alert-live"
    assert first["severity"] == "critical"
    assert first["created_at"] is None


async def test_list_does_not_repeat_a_stored_incident_held_in_short_term_memory(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    stored = await _insert_incident(pool, status="escalated", created_at=T0)
    stm = ShortTermMemory()
    stm.create(stored, _alert())

    items = (await _get(_app(stm, episodic), "/incidents")).json()["incidents"]

    assert [(item["id"], item["status"]) for item in items] == [(stored, "escalated")]


async def test_list_status_filter_selects_the_source(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    live = str(uuid.uuid4())
    stm = ShortTermMemory()
    stm.create(live, _alert())
    stored = await _insert_incident(pool, status="escalated", created_at=T0)
    app = _app(stm, episodic)

    in_progress = (await _get(app, "/incidents", status="in_progress")).json()["incidents"]
    escalated = (await _get(app, "/incidents", status="escalated")).json()["incidents"]

    assert [item["id"] for item in in_progress] == [live]
    assert [item["id"] for item in escalated] == [stored]


async def test_stuck_placeholder_listed_in_flight_never_as_stored(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    live = str(uuid.uuid4())
    stm = ShortTermMemory()
    stm.create(live, _alert())
    # A placeholder no pod is running (awaiting its resume), and one stored incident.
    stuck = await _insert_incident(pool, status="in_progress", created_at=T0)
    stored = await _insert_incident(pool, status="escalated", created_at=T0 - timedelta(hours=1))
    app = _app(stm, episodic)

    everything = (await _get(app, "/incidents")).json()["incidents"]
    in_progress = (await _get(app, "/incidents", status="in_progress")).json()["incidents"]
    escalated = (await _get(app, "/incidents", status="escalated")).json()["incidents"]

    # Unfiltered = in flight (running here, then the stuck placeholder) + stored: the union.
    assert [(i["id"], i["status"]) for i in everything] == [
        (live, "in_progress"),
        (stuck, "in_progress"),
        (stored, "escalated"),
    ]
    assert [i["id"] for i in in_progress] == [live, stuck]
    assert [i["id"] for i in escalated] == [stored]
    assert await episodic.stored_incident_ids([stuck, stored]) == {stored}  # never "stored"


async def test_list_service_filter_applies_to_in_flight(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    live, other = str(uuid.uuid4()), str(uuid.uuid4())
    stm = ShortTermMemory()
    stm.create(live, _alert("order-service"))
    stm.create(other, _alert("payment-service"))

    response = await _get(_app(stm, episodic), "/incidents", service="order-service")

    assert [item["id"] for item in response.json()["incidents"]] == [live]


# ── GET /incidents/{id} ───────────────────────────────────────────────────────


async def test_get_returns_every_column_of_a_stored_incident(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    correlation = uuid.uuid4()
    resolved_at = T0 + timedelta(minutes=10)
    incident_id = await _insert_incident(
        pool,
        status="rollback_proposed",
        created_at=T0,
        dd_event_id="dd-1",
        correlation_id=correlation,
        root_cause="pr-42",
        root_cause_confidence=0.9,
        resolution="revert",
        resolution_type="rollback",
        pr_number=43,
        pr_url="https://github.com/x/y/pull/43",
        trajectory={"pipeline_result": {"target_deploy": "a1b2c3d"}},
        eval_score={"accuracy": 4},
        langfuse_trace_id="f" * 32,
        reflexion_loops=2,
        resolved_at=resolved_at,
        mttr_seconds=600,
    )

    response = await _get(_app(ShortTermMemory(), episodic), f"/incidents/{incident_id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == INCIDENT_COLUMNS
    assert body["id"] == incident_id
    assert body["status"] == "rollback_proposed"
    assert body["service"] == "payment-service" and body["severity"] == "P1"
    assert body["dd_event_id"] == "dd-1"
    assert body["correlation_id"] == str(correlation)
    assert body["root_cause"] == "pr-42" and body["root_cause_confidence"] == pytest.approx(0.9)
    assert body["resolution"] == "revert" and body["resolution_type"] == "rollback"
    assert body["pr_number"] == 43 and body["pr_url"] == "https://github.com/x/y/pull/43"
    assert body["trajectory"] == {"pipeline_result": {"target_deploy": "a1b2c3d"}}
    assert body["eval_score"] == {"accuracy": 4}
    assert body["langfuse_trace_id"] == "f" * 32 and body["reflexion_loops"] == 2
    assert datetime.fromisoformat(body["created_at"]) == T0
    assert datetime.fromisoformat(body["resolved_at"]) == resolved_at
    assert body["mttr_seconds"] == 600


async def test_get_serves_an_in_flight_incident_from_short_term_memory(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    live = str(uuid.uuid4())
    stm = ShortTermMemory()
    stm.create(live, _alert())
    stm.add_timeline_entry(
        live,
        TimelineEntry(
            agent_name="triage_agent", action="get_service_metadata", result_summary="ok"
        ),
    )

    response = await _get(_app(stm, episodic), f"/incidents/{live}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "in_progress"
    assert body["trajectory"]["timeline"]["timeline"][0]["agent_name"] == "triage_agent"
    assert "pipeline_result" not in body["trajectory"]
    assert body["root_cause"] is None and body["pr_number"] is None
    assert body["eval_score"] is None


async def test_get_prefers_the_stored_row_over_short_term_memory(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    stored = await _insert_incident(pool, status="rollback_proposed", created_at=T0)
    stm = ShortTermMemory()
    stm.create(stored, _alert())

    body = (await _get(_app(stm, episodic), f"/incidents/{stored}")).json()

    assert body["status"] == "rollback_proposed"


@pytest.mark.parametrize("incident_id", [str(uuid.uuid4()), "not-a-uuid"])
async def test_get_unknown_id_is_404(
    episodic: EpisodicMemory, pool: asyncpg.Pool, incident_id: str
) -> None:
    response = await _get(_app(ShortTermMemory(), episodic), f"/incidents/{incident_id}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


# ── GET /incidents/{id}/trajectory ────────────────────────────────────────────


async def test_trajectory_returns_the_stored_jsonb(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    trajectory = {
        "symptoms": "s",
        "timeline": {"id": "x", "status": "escalated", "timeline": []},
    }
    incident_id = await _insert_incident(pool, created_at=T0, trajectory=trajectory)

    response = await _get(_app(ShortTermMemory(), episodic), f"/incidents/{incident_id}/trajectory")

    assert response.status_code == 200
    assert response.json() == {"incident_id": incident_id, "trajectory": trajectory}


async def test_trajectory_of_a_row_without_one_is_null(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    incident_id = await _insert_incident(pool, created_at=T0, trajectory=None)

    body = (
        await _get(_app(ShortTermMemory(), episodic), f"/incidents/{incident_id}/trajectory")
    ).json()

    assert body["trajectory"] is None


async def test_trajectory_of_an_in_flight_incident_is_its_live_timeline(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    live = str(uuid.uuid4())
    stm = ShortTermMemory()
    stm.create(live, _alert())
    stm.update_context(live, result={"target_deploy": "a1b2c3d"})

    body = (await _get(_app(stm, episodic), f"/incidents/{live}/trajectory")).json()

    assert body["incident_id"] == live
    assert body["trajectory"]["pipeline_result"] == {"target_deploy": "a1b2c3d"}
    assert body["trajectory"]["timeline"]["status"] == "in_progress"


async def test_trajectory_unknown_id_is_404(episodic: EpisodicMemory, pool: asyncpg.Pool) -> None:
    response = await _get(
        _app(ShortTermMemory(), episodic), f"/incidents/{uuid.uuid4()}/trajectory"
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


# ── GET /incidents/{id}/deploy ────────────────────────────────────────────────


async def test_deploy_returns_the_linked_deployments_row(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    incident_id = await _insert_incident(pool, created_at=T0)
    linked = await _insert_deployment(
        pool,
        incident_id=incident_id,
        pr_number=42,
        commit_sha="a1b2c3d4e5f6",
        author="dev",
        deploy_status="failed",
        gha_run_id=9876543210,
        dd_deploy_event_id="dd-dep-1",
        files_changed=["app/main.py"],
        metadata={"failed_stage": "verify"},
    )
    await _insert_deployment(pool, incident_id=None, commit_sha="ffff000")

    response = await _get(_app(ShortTermMemory(), episodic), f"/incidents/{incident_id}/deploy")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == DEPLOYMENT_COLUMNS
    assert body["id"] == linked
    assert body["incident_id"] == incident_id
    assert body["commit_sha"] == "a1b2c3d4e5f6"
    assert body["pr_number"] == 42
    assert body["gha_run_id"] == 9876543210
    assert body["files_changed"] == ["app/main.py"]
    assert body["metadata"] == {"failed_stage": "verify"}


async def test_deploy_without_a_linked_row_is_404(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    incident_id = await _insert_incident(pool, created_at=T0)
    await _insert_deployment(pool, incident_id=None)

    response = await _get(_app(ShortTermMemory(), episodic), f"/incidents/{incident_id}/deploy")

    assert response.status_code == 404
    assert "no deployment is linked" in response.json()["detail"].lower()


async def test_deploy_of_an_in_flight_incident_is_404_no_deployment(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    live = str(uuid.uuid4())
    stm = ShortTermMemory()
    stm.create(live, _alert())

    response = await _get(_app(stm, episodic), f"/incidents/{live}/deploy")

    assert response.status_code == 404
    assert "no deployment" in response.json()["detail"].lower()


async def test_deploy_unknown_incident_is_404(episodic: EpisodicMemory, pool: asyncpg.Pool) -> None:
    response = await _get(_app(ShortTermMemory(), episodic), f"/incidents/{uuid.uuid4()}/deploy")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


# ── GET /incidents/{id}/rollback-spec ─────────────────────────────────────────


def _spec(incident_id: str) -> RollbackSpec:
    return RollbackSpec(
        incident_id=incident_id,
        root_cause="bad deploy pr-12",
        evidence_summary="5xx after deploy",
        confidence=0.9,
        justification="revert pr-12",
        target_deploy=TargetDeploy(pr_number=12, commit_sha="a1b2c3d", deployed_at=T0),
    )


def _rollback_result(incident_id: str) -> dict[str, Any]:
    return PipelineResult(
        incident_id=incident_id,
        status="rollback_proposed",
        resolution_type="rollback",
        root_cause="bad deploy pr-12",
        confidence=0.9,
        rollback_spec=_spec(incident_id),
        reflexion_loops=0,
        tool_calls_used=3,
        tokens_used=100,
    ).model_dump(mode="json")


def _escalated_result(incident_id: str) -> dict[str, Any]:
    return PipelineResult(
        incident_id=incident_id,
        status="escalated",
        resolution_type="escalated",
        root_cause="unclear",
        confidence=0.4,
        escalation=EscalationContext(
            hypothesis="unclear", evidence=["no deploy"], confidence=0.4, missing=["deploy record"]
        ),
        reflexion_loops=0,
        tool_calls_used=3,
        tokens_used=100,
    ).model_dump(mode="json")


def _row(**kw: Any) -> IncidentRow:
    values: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "alert_id": "alert-1",
        "severity": "P1",
        "service": "payment-service",
        "status": "rollback_proposed",
    } | kw
    return IncidentRow(**values)


def test_rollback_spec_of_returns_the_spec() -> None:
    incident_id = str(uuid.uuid4())
    row = _row(id=incident_id, trajectory={"pipeline_result": _rollback_result(incident_id)})

    assert rollback_spec_of(row) == _spec(incident_id)


@pytest.mark.parametrize("status", ["escalated", "in_progress"])
def test_rollback_spec_of_is_none_unless_rollback_proposed(status: str) -> None:
    incident_id = str(uuid.uuid4())
    row = _row(
        id=incident_id,
        status=status,
        trajectory={"pipeline_result": _rollback_result(incident_id)},
    )

    assert rollback_spec_of(row) is None


@pytest.mark.parametrize(
    "trajectory",
    [None, [], {"x": 1}, {"pipeline_result": []}, "escalated"],
)
def test_rollback_spec_of_is_none_without_a_spec(trajectory: Any) -> None:
    incident_id = str(uuid.uuid4())
    if trajectory == "escalated":
        trajectory = {"pipeline_result": _escalated_result(incident_id)}

    assert rollback_spec_of(_row(id=incident_id, trajectory=trajectory)) is None


def test_rollback_spec_of_rejects_an_invalid_spec() -> None:
    incident_id = str(uuid.uuid4())
    result = _rollback_result(incident_id)
    result["rollback_spec"]["confidence"] = 2.0

    assert rollback_spec_of(_row(id=incident_id, trajectory={"pipeline_result": result})) is None


def test_rollback_spec_of_rejects_another_incidents_spec() -> None:
    incident_id = str(uuid.uuid4())
    result = _rollback_result(str(uuid.uuid4()))

    assert rollback_spec_of(_row(id=incident_id, trajectory={"pipeline_result": result})) is None


async def test_rollback_spec_returns_the_drafted_spec(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    incident_id = str(uuid.uuid4())
    await _insert_incident(
        pool,
        id=incident_id,
        status="rollback_proposed",
        created_at=T0,
        trajectory={"pipeline_result": _rollback_result(incident_id)},
    )

    response = await _get(
        _app(ShortTermMemory(), episodic), f"/incidents/{incident_id}/rollback-spec"
    )

    assert response.status_code == 200
    body = response.json()
    assert RollbackSpec.model_validate(body) == _spec(incident_id)
    assert body["target_deploy"]["commit_sha"] == "a1b2c3d"
    assert body["resolution_type"] == "rollback"


async def test_rollback_spec_of_an_escalated_incident_is_404(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    incident_id = str(uuid.uuid4())
    await _insert_incident(
        pool,
        id=incident_id,
        status="escalated",
        created_at=T0,
        trajectory={"pipeline_result": _escalated_result(incident_id)},
    )

    response = await _get(
        _app(ShortTermMemory(), episodic), f"/incidents/{incident_id}/rollback-spec"
    )

    assert response.status_code == 404
    assert "no rollback spec" in response.json()["detail"]


async def test_rollback_spec_without_a_trajectory_is_404(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    incident_id = await _insert_incident(pool, status="rollback_proposed", created_at=T0)

    response = await _get(
        _app(ShortTermMemory(), episodic), f"/incidents/{incident_id}/rollback-spec"
    )

    assert response.status_code == 404


@pytest.mark.parametrize("incident_id", [str(uuid.uuid4()), "not-a-uuid"])
async def test_rollback_spec_unknown_id_is_404(
    episodic: EpisodicMemory, pool: asyncpg.Pool, incident_id: str
) -> None:
    response = await _get(
        _app(ShortTermMemory(), episodic), f"/incidents/{incident_id}/rollback-spec"
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


async def test_rollback_spec_of_an_in_flight_incident_is_404(
    episodic: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    live = str(uuid.uuid4())
    stm = ShortTermMemory()
    stm.create(live, _alert())
    stm.update_context(live, result=_rollback_result(live))

    response = await _get(_app(stm, episodic), f"/incidents/{live}/rollback-spec")

    assert response.status_code == 404
