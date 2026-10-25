"""Tests for sentinel.api.generate — POST /generate/pr-content (backend.md §3.4).

Fake LLM only; the database tests run on a throwaway database of TEST_DATABASE_URL
(migrated to head, dropped afterwards; they skip with a reason when it is not set, and
fail in CI). No network beyond the test database.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import litellm
import pytest
from agents import MaxTurnsExceeded, ModelBehaviorError
from alembic import command
from alembic.config import Config
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute

from sentinel.api import generate as generate_module
from sentinel.api.generate import GENERATE_PATH, make_generate_router
from sentinel.config import Settings
from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.database import create_pool
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.memory import EpisodicRecord
from sentinel.models.tools import DeployRecord, RollbackSpec, TargetDeploy
from sentinel.providers import PR_CONTENT_AGENT, fake_script, pr_content_turn
from sentinel.tools.deploy_checker import TargetDeployCheck

HAIKU = "anthropic/claude-haiku-4-5"
GPT_MINI = "openai/gpt-4o-mini"
INCIDENT_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
SERVICE = "sentinel-watchtower"
SHORT_SHA = "a3f9c2e"
FULL_SHA = "a3f9c2e7b1d04f5a9c8e6b2d1f0a3c5e7b9d2f41"  # 40 hex, starts with SHORT_SHA
DEPLOYED_AT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
UNAVAILABLE = "the rollback target cannot be checked: the database is unavailable"


@pytest.fixture(autouse=True)
def local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name resolution for anything but localhost and the test database fails the test."""
    real = socket.getaddrinfo
    allowed = {"localhost", "127.0.0.1", "::1"}
    db_host = urlsplit(os.environ.get("TEST_DATABASE_URL", "")).hostname
    if db_host:
        allowed.add(db_host)

    def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name is not None and name not in allowed:
            raise AssertionError(f"network access attempted: {name}")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


def _settings(*, fake: bool = True) -> Settings:
    return Settings(_env_file=None, sentinel_fake_llm=fake)  # type: ignore[call-arg]


def _body(**overrides: Any) -> dict[str, Any]:
    """The §3.4 example body for INCIDENT_ID; a key overridden with None is removed."""
    body: dict[str, Any] = {
        "incident_id": INCIDENT_ID,
        "root_cause": "Deploy pr-5-a3f9c2 changed /health to return 503 when any "
        "dependency is degraded",
        "evidence_summary": "Verify stage failed 3/3 health checks post-deploy; Datadog logs "
        "show 503s starting at deploy time; no other deploys in the window",
        "confidence": 0.85,
        "justification": "Only deploy in the window; failure began at its deploy time; "
        "reverting restores the last succeeded version",
        "target_deploy": {
            "pr_number": 5,
            "commit_sha": SHORT_SHA,
            "deployed_at": "2026-07-01T12:00:00Z",
        },
        "resolution_type": "rollback",
    }
    for key, value in overrides.items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    return body


def _pinned_spec() -> RollbackSpec:
    """The spec the generator sees: the body with its target pinned to the verified row."""
    spec = RollbackSpec.model_validate(_body())
    target = TargetDeploy(pr_number=5, commit_sha=FULL_SHA, deployed_at=DEPLOYED_AT)
    return spec.model_copy(update={"target_deploy": target})


def _app(router: APIRouter, *dependencies: Callable[..., Any]) -> FastAPI:
    app = FastAPI()
    app.include_router(router, dependencies=[Depends(d) for d in dependencies])
    return app


async def _post(app: FastAPI, body: dict[str, Any]) -> httpx.Response:
    # ASGITransport (not TestClient): the handler runs in this task, so the fake_script
    # ContextVar reaches the fake model.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(GENERATE_PATH, json=body)


class _StubEpisodic:
    """Stands in for EpisodicMemory: records the ids it is asked for."""

    def __init__(self, record: EpisodicRecord | None) -> None:
        self._record = record
        self.asked: list[str] = []

    async def get_incident(self, incident_id: str) -> EpisodicRecord | None:
        self.asked.append(incident_id)
        return self._record


def _incident(service: str = SERVICE) -> EpisodicRecord:
    return EpisodicRecord(
        id=INCIDENT_ID,
        service_name=service,
        severity="P2",
        symptoms="",
        root_cause="unknown",
        resolution="unresolved",
    )


def _verified_record() -> DeployRecord:
    return DeployRecord(
        pr_number=5, commit_sha=FULL_SHA, deploy_status="failed", deployed_at=DEPLOYED_AT
    )


def _stub_check(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, TargetDeploy]]:
    """Replace the cross-check with one that verifies every target; returns its calls."""
    seen: list[tuple[str, TargetDeploy]] = []

    async def fake_check(pool: object, service: str, target: TargetDeploy) -> TargetDeployCheck:
        seen.append((service, target))
        return TargetDeployCheck(deploy_id="d-1", record=_verified_record())

    monkeypatch.setattr(generate_module, "check_rollback_target", fake_check)
    return seen


def _stub_router(stub: _StubEpisodic, *, pool: Any = None, fake: bool = True) -> APIRouter:
    return make_generate_router(
        episodic_memory=stub,  # type: ignore[arg-type]
        pool=pool,
        settings=_settings(fake=fake),
    )


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
def generate_dsn(
    pg_dsn: str,
    _module_db: dict[str, str],
    alembic_config: Callable[..., Config],
    seed_embedding_client: EmbeddingClient,
) -> str:
    """A database at head with no incidents and no deployments."""
    if not _module_db:
        name = f"sentinel_generate_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"], seed_embedding_client), "head")
    _execute(_module_db["dsn"], "DELETE FROM deployments; DELETE FROM incidents")
    return _module_db["dsn"]


@pytest.fixture
async def pool(generate_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(generate_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def _seed_incident(
    pool: asyncpg.Pool, *, incident_id: str = INCIDENT_ID, service: str = SERVICE
) -> None:
    await pool.execute(
        "INSERT INTO incidents (id, alert_id, severity, service, status) "
        "VALUES ($1, 'alert-1', 'P2', $2, 'rollback_proposed')",
        uuid.UUID(incident_id),
        service,
    )


async def _seed_deploy(
    pool: asyncpg.Pool,
    *,
    service: str = SERVICE,
    pr: int = 5,
    sha: str = FULL_SHA,
    deployed_at: datetime = DEPLOYED_AT,
) -> None:
    await pool.execute(
        "INSERT INTO deployments (service, pr_number, commit_sha, author, deploy_status, "
        "gha_run_id, files_changed, deployed_at, metadata) "
        "VALUES ($1, $2, $3, 'keshav', 'failed', 1001, '[\"app/main.py\"]'::jsonb, $4, "
        '\'{"failed_stage": "verify", "version": "pr-5-a3f9c2e"}\'::jsonb)',
        service,
        pr,
        sha,
        deployed_at,
    )


def _db_router(pool: asyncpg.Pool) -> APIRouter:
    return make_generate_router(
        episodic_memory=EpisodicMemory(pool, MagicMock()), pool=pool, settings=_settings()
    )


# ── No database needed ────────────────────────────────────────────────────────


def test_router_carries_no_auth_of_its_own() -> None:
    router = make_generate_router(
        episodic_memory=EpisodicMemory(None, MagicMock()), pool=None, settings=_settings()
    )
    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    assert len(routes) == 1
    assert routes[0].path == "/generate/pr-content"
    assert routes[0].methods == {"POST"}
    assert router.dependencies == []
    assert routes[0].dependencies == []


async def test_a_mount_dependency_guards_the_route() -> None:
    async def deny() -> None:
        raise HTTPException(status_code=401, detail="missing bearer")

    router = make_generate_router(
        episodic_memory=EpisodicMemory(None, MagicMock()), pool=None, settings=_settings()
    )
    with fake_script() as script:
        response = await _post(_app(router, deny), _body())
    assert response.status_code == 401
    assert script.calls == []


async def test_no_database_is_503() -> None:
    router = make_generate_router(
        episodic_memory=EpisodicMemory(None, MagicMock()), pool=None, settings=_settings()
    )
    with fake_script() as script:
        response = await _post(_app(router), _body())
    assert response.status_code == 503
    assert "no database is configured" in response.json()["detail"]
    assert script.calls == []


async def test_an_unavailable_incident_store_is_503() -> None:
    router = make_generate_router(
        episodic_memory=EpisodicMemory(None, MagicMock()), pool=MagicMock(), settings=_settings()
    )
    with fake_script() as script:
        response = await _post(_app(router), _body())
    assert response.status_code == 503
    assert response.json()["detail"] == "the incident store is unavailable"
    assert script.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"resolution_type": "hotfix"},
        {"justification": None},
        {"confidence": 1.5},
        {"target_deploy": None},
    ],
    ids=["not-a-rollback", "missing-justification", "confidence-above-1", "missing-target"],
)
async def test_the_body_is_a_rollback_spec(overrides: dict[str, Any]) -> None:
    stub = _StubEpisodic(_incident())
    with fake_script() as script:
        response = await _post(_app(_stub_router(stub, pool=MagicMock())), _body(**overrides))
    assert response.status_code == 422
    assert stub.asked == []
    assert script.calls == []


async def test_the_service_comes_from_the_stored_incident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubEpisodic(_incident(service="other-svc"))
    seen = _stub_check(monkeypatch)
    with fake_script() as script:
        script.on(PR_CONTENT_AGENT, pr_content_turn(_pinned_spec()))
        response = await _post(_app(_stub_router(stub, pool=MagicMock())), _body())
    assert response.status_code == 200
    assert stub.asked == [INCIDENT_ID]
    assert seen == [
        ("other-svc", TargetDeploy(pr_number=5, commit_sha=SHORT_SHA, deployed_at=DEPLOYED_AT))
    ]


async def test_no_llm_provider_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubEpisodic(_incident())
    _stub_check(monkeypatch)
    with fake_script():
        response = await _post(_app(_stub_router(stub, pool=MagicMock(), fake=False)), _body())
    assert response.status_code == 503
    assert response.json()["detail"] == "no LLM provider is configured"


async def test_a_provider_failure_after_fallback_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubEpisodic(_incident())
    _stub_check(monkeypatch)
    with fake_script() as script:
        script.fail_on(HAIKU, litellm.RateLimitError("429", llm_provider="anthropic", model=HAIKU))
        script.fail_on(
            GPT_MINI, litellm.RateLimitError("429", llm_provider="openai", model=GPT_MINI)
        )
        response = await _post(_app(_stub_router(stub, pool=MagicMock())), _body())
    assert response.status_code == 502
    assert response.json()["detail"] == "the LLM provider failed to generate the PR content"
    assert [m for _, m in script.calls] == [HAIKU] * 3 + [GPT_MINI] * 3


@pytest.mark.parametrize(
    "exc", [ModelBehaviorError("bad output"), MaxTurnsExceeded("too many turns")]
)
async def test_an_agents_sdk_error_is_502(exc: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubEpisodic(_incident())
    _stub_check(monkeypatch)
    monkeypatch.setattr(generate_module, "generate_pr_content", AsyncMock(side_effect=exc))

    response = await _post(_app(_stub_router(stub, pool=MagicMock())), _body())

    assert response.status_code == 502
    assert response.json()["detail"] == "the LLM provider failed to generate the PR content"


async def test_a_database_failure_during_the_cross_check_is_503() -> None:
    broken = MagicMock()
    broken.acquire = AsyncMock(side_effect=OSError("connection refused"))
    stub = _StubEpisodic(_incident())
    with fake_script() as script:
        response = await _post(_app(_stub_router(stub, pool=broken)), _body())
    assert response.status_code == 503
    assert response.json()["detail"] == UNAVAILABLE
    assert "connection refused" not in response.text
    assert script.calls == []


async def test_an_unavailable_check_result_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable(pool: object, service: str, target: TargetDeploy) -> TargetDeployCheck:
        return TargetDeployCheck(
            problem="the rollback target cannot be checked: x", unavailable=True
        )

    monkeypatch.setattr(generate_module, "check_rollback_target", unavailable)
    stub = _StubEpisodic(_incident())
    with fake_script() as script:
        response = await _post(_app(_stub_router(stub, pool=MagicMock())), _body())
    assert response.status_code == 503
    assert response.json()["detail"] == UNAVAILABLE
    assert script.calls == []


async def test_a_raising_cross_check_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def raising(pool: object, service: str, target: TargetDeploy) -> TargetDeployCheck:
        raise SentinelMemoryError("boom")

    monkeypatch.setattr(generate_module, "check_rollback_target", raising)
    stub = _StubEpisodic(_incident())
    with fake_script() as script:
        response = await _post(_app(_stub_router(stub, pool=MagicMock())), _body())
    assert response.status_code == 503
    assert response.json()["detail"] == UNAVAILABLE
    assert "boom" not in response.text
    assert script.calls == []


# ── Against the test database ─────────────────────────────────────────────────


async def test_a_verified_target_gets_the_pr_content(pool: asyncpg.Pool) -> None:
    await _seed_incident(pool)
    await _seed_deploy(pool)
    with fake_script() as script:
        script.on(PR_CONTENT_AGENT, pr_content_turn(_pinned_spec(), usage=(900, 120)))
        response = await _post(_app(_db_router(pool)), _body())
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"title", "description", "model_used", "tokens_used"}
    assert data["title"].startswith("revert:")
    assert len(data["title"]) < 72
    assert "#5" in data["title"] and INCIDENT_ID in data["title"]
    assert data["description"].startswith("## Incident")
    assert data["model_used"] == HAIKU
    assert data["tokens_used"] == 1020
    assert script.calls == [(PR_CONTENT_AGENT, HAIKU)]


async def test_the_pr_text_names_the_verified_full_sha(pool: asyncpg.Pool) -> None:
    await _seed_incident(pool)
    await _seed_deploy(pool)
    with fake_script() as script:
        script.on(PR_CONTENT_AGENT, pr_content_turn(_pinned_spec(), compliant=False))
        response = await _post(_app(_db_router(pool)), _body())
    assert response.status_code == 200
    data = response.json()
    # The reply broke every rule, so the deterministic fallback text is what comes back.
    assert f"reverts commit {FULL_SHA} from PR #5" in data["description"]
    assert data["title"] == f"revert: roll back PR #5 (incident {INCIDENT_ID})"


@pytest.mark.parametrize("incident_id", [str(uuid.uuid4()), "inc-uuid"], ids=["absent", "not-uuid"])
async def test_an_unknown_incident_is_404(pool: asyncpg.Pool, incident_id: str) -> None:
    await _seed_incident(pool)
    await _seed_deploy(pool)
    with fake_script() as script:
        response = await _post(_app(_db_router(pool)), _body(incident_id=incident_id))
    assert response.status_code == 404
    assert incident_id in response.json()["detail"]
    assert script.calls == []


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        (None, f"no deployments row for {SERVICE} has commit {SHORT_SHA}"),
        ({"service": "other-service"}, f"no deployments row for {SERVICE}"),
        ({"pr": 4}, "was recorded as PR #4"),
        ({"deployed_at": DEPLOYED_AT - timedelta(minutes=5)}, "but the rollback spec says PR #5"),
    ],
    ids=["no-row", "other-service", "other-pr", "other-time"],
)
async def test_an_unverified_target_is_409(
    pool: asyncpg.Pool, row: dict[str, Any] | None, problem: str
) -> None:
    await _seed_incident(pool)
    if row is not None:
        await _seed_deploy(pool, **row)
    with fake_script() as script:
        response = await _post(_app(_db_router(pool)), _body())
    assert response.status_code == 409
    assert problem in response.json()["detail"]
    assert script.calls == []
