"""Fixtures for the offline end-to-end smoke (tests/test_e2e/test_e2e.py).

The real ``main.create_app()`` and ``main.lifespan`` run against a throwaway database migrated
to 001 and are driven through an httpx ASGI transport, on the fake LLM. What is replaced is
only what would leave the process: the embedding model, the JWKS fetch and the LangFuse
dataset client. An autouse guard fails the test on any outbound request that is not to the
app itself, the test database or loopback.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import jwt
import pytest
from agents import set_trace_processors
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from sentinel import main
from sentinel.agents import orchestrator
from sentinel.agents.orchestrator import REFLEXION_MAX_LOOPS
from sentinel.api import auth
from sentinel.catalog import CATALOG_PATH
from sentinel.eval.dataset_writer import LangfuseScenarioWriter
from sentinel.infra.database import create_pool
from sentinel.infra.incident_tasks import IncidentTasks
from sentinel.models.analysis import AnalysisResult
from sentinel.models.deploy import CorrelatedDeploy
from sentinel.models.incident import Severity, TriageResult
from sentinel.models.reflexion import SuspectDeploy
from sentinel.providers import (
    REFLEXION_AGENT,
    REVERIFICATION_AGENT,
    FakeScript,
    FakeToolCall,
    FakeTurn,
    reflexion_turn,
    reverification_turn,
)

SERVICE = "sentinel-watchtower"
TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
DEPLOYED_AT = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
SHA_A = "3" * 40  # PR 31, deployfail/03
SHA_B = "4" * 40  # PR 32, runtime/01
SHA_C = "5" * 40  # PR 33, runtime/01
OTHER = "6" * 40
APP_HOST = "test"

# ── The test database ─────────────────────────────────────────────────────────


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
def e2e_dsn(pg_dsn: str, _module_db: dict[str, str], alembic_config: Callable[..., Config]) -> str:
    """A database at migration 001 with ``deployments`` and ``incidents`` emptied."""
    if not _module_db:
        name = f"sentinel_e2e_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"]), "001")
    _execute(_module_db["dsn"], "DELETE FROM deployments; DELETE FROM incidents")
    return _module_db["dsn"]


class Embedder:
    """A 384-dim embedding client stand-in: one fixed non-zero vector, no model load."""

    async def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 383


# ── LangFuse ──────────────────────────────────────────────────────────────────


class _Span:
    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id


class FakeDatasetItem:
    """A dataset item as ``get_dataset`` returns it: id, input and ``run``."""

    def __init__(self, id: str, input: Any) -> None:  # noqa: A002 - the SDK's field names
        self.id = id
        self.input = input

    @contextmanager
    def run(
        self,
        *,
        run_name: str,
        run_metadata: Any | None = None,
        run_description: str | None = None,
    ) -> Iterator[_Span]:
        yield _Span(f"trace-{self.id}")


class FakeLangfuse:
    """The dataset half of the LangFuse client: written by the pipeline, read by the eval."""

    def __init__(self) -> None:
        self.datasets: dict[str, list[FakeDatasetItem]] = {}
        self.item_kwargs: list[dict[str, Any]] = []

    def create_dataset(self, *, name: str, description: str | None = None, **_: Any) -> None:
        self.datasets.setdefault(name, [])

    def create_dataset_item(
        self,
        *,
        dataset_name: str,
        input: Any = None,  # noqa: A002 - the SDK's parameter name
        id: str | None = None,  # noqa: A002 - the SDK's parameter name
        **kwargs: Any,
    ) -> None:
        if dataset_name not in self.datasets:
            raise KeyError(dataset_name)
        item_id = id or str(uuid.uuid4())
        self.datasets[dataset_name].append(FakeDatasetItem(item_id, json.loads(json.dumps(input))))
        self.item_kwargs.append({"id": item_id, "dataset_name": dataset_name, **kwargs})

    def get_dataset(self, name: str) -> SimpleNamespace:
        return SimpleNamespace(items=list(self.datasets[name]))

    def items(self, name: str = "sentinel-scenario-runs") -> list[FakeDatasetItem]:
        return list(self.datasets.get(name, []))


# ── No outbound network ───────────────────────────────────────────────────────


class OutboundBlocked(AssertionError):
    """A test reached for a host that is neither the app, the test database nor loopback."""


class OutboundGuard:
    """Records, and refuses, every outbound HTTP request and socket connect (see conftest)."""

    def __init__(self, allowed_ips: set[str]) -> None:
        self.allowed_ips = allowed_ips
        self.attempts: list[str] = []

    def check_http(self, host: str) -> None:
        if host != APP_HOST:
            self.attempts.append(host)
            raise OutboundBlocked(f"outbound HTTP request to {host}")

    def check_socket(self, address: Any) -> None:
        if isinstance(address, (str, bytes)):  # AF_UNIX
            return
        host = str(address[0])
        if host not in self.allowed_ips:
            self.attempts.append(host)
            raise OutboundBlocked(f"outbound connection to {host}")


def _allowed_ips() -> set[str]:
    allowed = {"127.0.0.1", "::1"}
    host = urlsplit(os.environ.get("TEST_DATABASE_URL", "")).hostname
    if host:
        try:
            for info in socket.getaddrinfo(host, None):
                allowed.add(str(info[4][0]))
        except OSError:
            pass
    return allowed


@pytest.fixture(autouse=True)
def outbound(monkeypatch: pytest.MonkeyPatch) -> Iterator[OutboundGuard]:
    """Fail on any request to a host but the app (``test``), the test database or loopback.

    That covers github.com, Teams and Datadog, and is stricter: nothing else is reachable.
    """
    guard = OutboundGuard(_allowed_ips())
    real_send = httpx.Client.send
    real_async_send = httpx.AsyncClient.send
    real_connect = socket.socket.connect

    def send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        guard.check_http(request.url.host)
        return real_send(self, request, **kwargs)

    async def async_send(
        self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        guard.check_http(request.url.host)
        return await real_async_send(self, request, **kwargs)

    def connect(self: socket.socket, address: Any) -> None:
        guard.check_socket(address)
        real_connect(self, address)

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    monkeypatch.setattr(socket.socket, "connect", connect)
    yield guard
    assert guard.attempts == [], f"outbound attempts: {guard.attempts}"


# ── The app ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def rsa_key() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJWKS(jwt.PyJWKClient):
    """PyJWKClient whose key set is in-memory."""

    def __init__(self, keys: list[dict[str, Any]]) -> None:
        super().__init__("https://login.microsoftonline.com/x/discovery/v2.0/keys")
        self._keys = keys

    def fetch_data(self) -> Any:
        self._last_successful_fetch = time.monotonic()
        return {"keys": self._keys}


def _bearer(key: RSAPrivateKey) -> dict[str, str]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "aud": CLIENT_ID,
        "iss": ISSUER,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "sub": "sub-1",
        "tid": TENANT,
        "roles": ["Incident.Write"],
    }
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})
    return {"Authorization": f"Bearer {token}"}


async def drain(tasks: IncidentTasks) -> None:
    """Wait for every in-flight incident task."""
    pending = list(tasks)
    if pending:
        await asyncio.gather(*pending)


@dataclass
class E2E:
    """The running app and what the tests look at."""

    client: httpx.AsyncClient
    pool: asyncpg.Pool
    langfuse: FakeLangfuse
    tasks: IncidentTasks
    dsn: str
    started: list[IncidentTasks] = field(default_factory=list[IncidentTasks])


@pytest.fixture
async def e2e(
    e2e_dsn: str,
    rsa_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[E2E]:
    """The real app, lifespan included, on the fake LLM and *e2e_dsn*."""
    monkeypatch.chdir(tmp_path)  # SentinelTracer writes reports/trajectories relative to cwd
    monkeypatch.setenv("DATABASE_URL", e2e_dsn)
    monkeypatch.setenv("SENTINEL_FAKE_LLM", "1")
    monkeypatch.setenv("SENTINEL_API_TENANT_ID", TENANT)
    monkeypatch.setenv("SENTINEL_API_AUDIENCE", CLIENT_ID)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True) | {
        "kid": "k1",
        "use": "sig",
        "alg": "RS256",
    }
    monkeypatch.setattr(auth, "_build_jwks_client", lambda tenant_id: _FakeJWKS([jwk]))
    monkeypatch.setattr(
        main, "EmbeddingClient", SimpleNamespace(from_model_name=lambda _name: Embedder())
    )

    langfuse = FakeLangfuse()
    writer = LangfuseScenarioWriter(client=langfuse)
    monkeypatch.setattr(orchestrator, "default_writer", lambda _settings: writer)

    started: list[IncidentTasks] = []

    class CapturedTasks(IncidentTasks):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            started.append(self)

    monkeypatch.setattr(main, "IncidentTasks", CapturedTasks)
    # configure_logging caches every logger on first use, which would blind capture_logs in
    # every later test of the session; the lifespan and run_eval keep the test's config.
    monkeypatch.setattr(main, "configure_logging", lambda *_a, **_k: None)
    monkeypatch.setattr("sentinel.eval.runner.configure_logging", lambda *_a, **_k: None)

    auth.reset_auth()
    app = main.create_app()
    pool = await create_pool(e2e_dsn, min_size=1, max_size=2)
    try:
        async with main.lifespan(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=f"http://{APP_HOST}",
                headers=_bearer(rsa_key),
            ) as client:
                yield E2E(client, pool, langfuse, started[0], e2e_dsn, started)
    finally:
        await pool.close()
        auth.reset_auth()
        set_trace_processors([])


# ── Data and script builders ──────────────────────────────────────────────────


async def insert_deploy(pool: asyncpg.Pool, pr: int, sha: str, status: str) -> None:
    """A ``deployments`` row of SERVICE, as the deploy recorder writes it."""
    await pool.execute(
        """
        INSERT INTO deployments (service, pr_number, commit_sha, deploy_status, deployed_at,
                                 files_changed, metadata)
        VALUES ($1, $2, $3, $4, $5, '["requirements.txt"]'::jsonb,
                jsonb_build_object('version', $6::text))
        """,
        SERVICE,
        pr,
        sha,
        status,
        DEPLOYED_AT,
        f"pr-{pr}-{sha[:7]}",
    )


def payload(
    signal_type: str, pr: int, sha: str, branch: str, cid: str | None = None
) -> dict[str, Any]:
    """The ``POST /webhooks/incident`` body ci_incident_response.yml sends for a PR."""
    tags: dict[str, str] = {"service": SERVICE, "version": f"pr-{pr}-{sha[:7]}"}
    if signal_type == "deploy_failure":
        tags |= {"deploy_status": "failed", "failed_stage": "deploy"}
    return {
        "signal_type": signal_type,
        "correlation_id": cid or str(uuid.uuid4()),
        "title": f"Synthetic check failed on {SERVICE}",
        "source": "datadog",
        "alert_id": f"alert-{uuid.uuid4().hex[:8]}",
        "dd_event_id": f"evt-{uuid.uuid4().hex[:8]}",
        "severity": "critical",
        "tags": tags,
        "context": {
            "pr_details": {
                "number": pr,
                "sha": sha,
                "author": "keshav",
                "files_changed": ["requirements.txt"],
                "branch": branch,
            }
        },
        "timestamp": "2026-10-25T12:05:00Z",
        "raw_payload": {"k": "v"},
    }


def _triage() -> FakeTurn:
    return FakeTurn(
        output=TriageResult(
            severity=Severity.P2,
            affected_service=SERVICE,
            is_duplicate=False,
            recommended_action="check the last deploy",
        )
    )


def _suspect(pr: int, sha: str, status: str) -> CorrelatedDeploy:
    return CorrelatedDeploy(
        pr_number=pr, commit_sha=sha[:7], deployed_at=DEPLOYED_AT, deploy_status=status
    )


def script_rollback(script: FakeScript, pr: int, sha: str) -> None:
    """A high-confidence critique of (pr, sha), a rollback draft and a PASSing reverification."""
    spec = {
        "incident_id": str(uuid.uuid4()),
        "root_cause": f"pr-{pr} changed the service so that every check fails",
        "evidence_summary": f"every check failed after pr-{pr} deployed",
        "confidence": 0.9,
        "justification": "the previous version passed the same checks",
        "target_deploy": {
            "pr_number": pr,
            "commit_sha": sha[:7],
            "deployed_at": DEPLOYED_AT.isoformat(),
        },
    }
    script.on("triage", _triage())
    script.on(
        "analysis",
        FakeTurn(tool_calls=[FakeToolCall("fetch_logs", {"request": {"service": SERVICE}})]),
        FakeTurn(
            output=AnalysisResult(
                hypothesis=f"the service fails since pr-{pr}",
                evidence=[f"fetch_logs: every check failed after pr-{pr}"],
                suspect_deploy=_suspect(pr, sha, "succeeded"),
            )
        ),
    )
    script.on(
        REFLEXION_AGENT,
        reflexion_turn(
            0.85,
            deploy=SuspectDeploy(commit_sha=sha[:7], pr_number=pr),
            supporting_evidence=[f"checks fail since pr-{pr}"],
        ),
    )
    script.on(
        "resolution",
        FakeTurn(tool_calls=[FakeToolCall("prepare_rollback_spec", {"request": spec})]),
        FakeTurn(output=f"Drafted the rollback of PR #{pr}."),
    )
    script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))


def script_low(script: FakeScript, pr: int, sha: str) -> None:
    """Every critique stays below 0.7: the loop runs out and the incident escalates."""
    passes = REFLEXION_MAX_LOOPS + 1
    script.on("triage", _triage())
    script.on(
        "analysis",
        *(
            FakeTurn(
                output=AnalysisResult(
                    hypothesis=f"maybe pr-{pr}, pass {n}",
                    evidence=[f"fetch_logs: some checks failed (pass {n})"],
                    suspect_deploy=_suspect(pr, sha, "succeeded"),
                )
            )
            for n in range(passes)
        ),
    )
    script.on(
        REFLEXION_AGENT,
        *(
            reflexion_turn(0.4, deploy=SuspectDeploy(commit_sha=sha[:7], pr_number=pr))
            for _ in range(passes)
        ),
    )


async def post_and_finish(e2e: E2E, body: dict[str, Any]) -> str:
    """POST the incident, wait for the pipeline, return the incident id."""
    response = await e2e.client.post("/webhooks/incident", json=body)
    assert response.status_code == 202, response.text
    ack = response.json()
    assert ack["status"] == "accepted"
    await drain(e2e.tasks)
    return str(ack["incident_id"])


def catalog_path() -> Path:
    """The scenario catalog the eval scores against (the one the package ships)."""
    return CATALOG_PATH
