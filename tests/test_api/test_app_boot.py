"""Tests for the real app lifespan (sentinel.main): route auth, the dev flag, Key Vault and PG*
wiring, and teardown order.

Nothing reaches Azure, a database or a network: the credential, SecretClient and JWKS fetch
are replaced, and tokens are signed with a throwaway RSA key.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import fastapi.routing
import jwt
import pytest
from azure.core.credentials import AccessToken
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sentinel import main
from sentinel.api import auth
from sentinel.api.auth import require_incident_write
from sentinel.config import Settings
from sentinel.eval.dataset_writer import DatasetWrites, TerminalRun
from sentinel.infra import database, workload_identity
from sentinel.infra import incident_tasks as tasks_module
from sentinel.infra.workload_identity import POSTGRES_ENTRA_SCOPE
from sentinel.models.alert import IncidentAlert
from sentinel.models.pipeline import PipelineInput, PipelineResult
from sentinel.models.tools import EscalationContext

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

_WEBHOOK_BODY = {"signal_type": "runtime_error", "correlation_id": str(uuid4()), "title": "t"}
_GENERATE_BODY = {
    "incident_id": "inc-x",
    "root_cause": "r",
    "evidence_summary": "e",
    "confidence": 0.9,
    "justification": "j",
    "target_deploy": {
        "pr_number": 1,
        "commit_sha": "a" * 40,
        "deployed_at": "2026-10-01T00:00:00Z",
    },
}

PROTECTED_REQUESTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/", None),
    ("GET", "/eval", None),
    ("GET", "/api/config", None),
    ("GET", "/api/scenarios", None),
    ("GET", "/api/trajectories", None),
    ("GET", "/api/eval-report", None),
    ("GET", "/incidents", None),
    ("GET", "/incidents/inc-x", None),
    ("GET", "/incidents/inc-x/trajectory", None),
    ("GET", "/incidents/inc-x/deploy", None),
    ("POST", "/webhooks/incident", _WEBHOOK_BODY),
    ("POST", "/generate/pr-content", _GENERATE_BODY),
]

EXPECTED_DEV_OPEN = {
    "GET /",
    "GET /eval",
    "GET /api/scenarios",
    "GET /api/config",
    "GET /api/trajectories",
    "GET /api/eval-report",
}

# ── Helpers / fixtures ────────────────────────────────────────────────────────


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


def _token(key: RSAPrivateKey, **overrides: Any) -> str:
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
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def _route_auth(app: FastAPI) -> dict[str, bool]:
    """{"METHOD path": True when require_incident_write guards it} for every API route."""
    out: dict[str, bool] = {}
    for ctx in fastapi.routing.iter_route_contexts(app.routes):
        dependant = getattr(ctx, "dependant", None)
        if dependant is None:
            continue
        guarded = any(d.call is require_incident_write for d in dependant.dependencies)
        for method in ctx.methods or ():
            out[f"{method} {ctx.path}"] = guarded
    return out


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def boot_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, MagicMock]]:
    """Real lifespan with the embedding model, trace processors, SDK defaults and Azure stubbed."""
    monkeypatch.setattr(main, "EmbeddingClient", MagicMock())
    monkeypatch.setattr(main, "install_trace_processors", MagicMock())
    monkeypatch.setattr(main, "apply_sdk_defaults", MagicMock())
    azure = {"credential": MagicMock(), "secret_client": MagicMock()}
    monkeypatch.setattr(workload_identity, "DefaultAzureCredential", azure["credential"])
    monkeypatch.setattr(workload_identity, "SecretClient", azure["secret_client"])
    auth.reset_auth()
    yield azure
    auth.reset_auth()


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch, rsa_key: RSAPrivateKey) -> RSAPrivateKey:
    monkeypatch.setenv("SENTINEL_API_TENANT_ID", TENANT)
    monkeypatch.setenv("SENTINEL_API_AUDIENCE", CLIENT_ID)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True) | {
        "kid": "k1",
        "use": "sig",
        "alg": "RS256",
    }
    monkeypatch.setattr(auth, "_build_jwks_client", lambda tenant_id: _FakeJWKS([jwk]))
    return rsa_key


# ── Probes and route auth ─────────────────────────────────────────────────────


def test_boots_with_no_env_and_probes_are_open() -> None:
    with TestClient(main.create_app()) as client:
        health = client.get("/health")
        ready = client.get("/ready")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED_REQUESTS)
def test_protected_routes_401_without_token(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    with TestClient(main.create_app()) as client:
        response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid token"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_only_probes_are_open_by_default() -> None:
    app = main.create_app()
    with TestClient(app):
        routes = _route_auth(app)
    assert {k for k, v in routes.items() if not v} == {"GET /health", "GET /ready"}
    must_be_guarded = {
        "POST /webhooks/incident",
        "POST /generate/pr-content",
        "GET /incidents",
        "GET /incidents/{incident_id}",
        "GET /incidents/{incident_id}/trajectory",
        "GET /incidents/{incident_id}/deploy",
    } | EXPECTED_DEV_OPEN
    for key in must_be_guarded:
        assert routes.get(key) is True, key


def test_valid_token_is_200_on_protected(auth_env: RSAPrivateKey) -> None:
    headers = _auth(_token(auth_env))
    with TestClient(main.create_app()) as client:
        config = client.get("/api/config", headers=headers)
        page = client.get("/", headers=headers)
    assert config.status_code == 200
    assert config.json()["roles"]["triage"]["primary"] == "anthropic/claude-haiku-4-5"
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")


def test_token_without_role_is_403(auth_env: RSAPrivateKey) -> None:
    with TestClient(main.create_app()) as client:
        response = client.get("/api/config", headers=_auth(_token(auth_env, roles=None)))
    assert response.status_code == 403


def test_token_for_other_audience_is_401(auth_env: RSAPrivateKey) -> None:
    with TestClient(main.create_app()) as client:
        response = client.get("/api/config", headers=_auth(_token(auth_env, aud="api://other")))
    assert response.status_code == 401


# ── SENTINEL_DEV_OPEN_DASHBOARD ───────────────────────────────────────────────


def test_dev_flag_opens_only_dashboard_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_DEV_OPEN_DASHBOARD", "1")
    app = main.create_app()
    with TestClient(app) as client:
        open_routes = {k for k, v in _route_auth(app).items() if not v}
        assert open_routes == {"GET /health", "GET /ready"} | EXPECTED_DEV_OPEN
        assert client.get("/").status_code == 200
        assert client.get("/eval").status_code == 200
        assert client.get("/api/config").status_code == 200
        assert client.get("/api/scenarios").status_code == 200
        assert client.post("/webhooks/incident", json=_WEBHOOK_BODY).status_code == 401
        assert client.post("/generate/pr-content", json=_GENERATE_BODY).status_code == 401
        assert client.get("/incidents").status_code == 401


def test_dev_flag_logs_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(main, "logger", log)

    def warned() -> bool:
        return any(c.args and c.args[0] == "dev_open_dashboard" for c in log.warning.call_args_list)

    monkeypatch.setenv("SENTINEL_DEV_OPEN_DASHBOARD", "1")
    with TestClient(main.create_app()):
        pass
    assert warned()

    log.reset_mock()
    monkeypatch.delenv("SENTINEL_DEV_OPEN_DASHBOARD")
    with TestClient(main.create_app()):
        pass
    assert not warned()


def test_dashboard_js_uses_only_dev_open_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every URL the dashboard JS calls is a dev-open GET; it POSTs nothing."""
    index_html = main._DASHBOARD_HTML.read_text(encoding="utf-8")
    eval_html = main._EVAL_HTML.read_text(encoding="utf-8")
    gets: set[str] = set()
    posts: set[str] = set()
    for html in (index_html, eval_html):
        for url, opts in re.findall(r"fetch\('([^']+)'(\s*,\s*\{[^}]*\})?", html):
            if "method:'POST'" in opts.replace(" ", ""):
                posts.add(url)
            else:
                gets.add(f"GET {url}")
    assert gets == {
        "GET /api/scenarios",
        "GET /api/config",
        "GET /api/trajectories",
        "GET /api/eval-report",
    }
    assert gets <= EXPECTED_DEV_OPEN
    assert posts == set()
    assert "EventSource" not in index_html
    assert "EventSource" not in eval_html

    monkeypatch.setenv("SENTINEL_DEV_OPEN_DASHBOARD", "1")
    app = main.create_app()
    with TestClient(app):
        assert _route_auth(app)["POST /webhooks/incident"] is True


def test_no_azure_at_boot_without_config(boot_env: dict[str, MagicMock]) -> None:
    with TestClient(main.create_app()) as client:
        assert client.get("/health").status_code == 200
    assert boot_env["credential"].call_count == 0
    assert boot_env["secret_client"].call_count == 0


def test_no_sse_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """No /events route; the dev flag is on, so the 404 cannot be an auth response."""
    monkeypatch.setenv("SENTINEL_DEV_OPEN_DASHBOARD", "1")
    app = main.create_app()
    with TestClient(app) as client:
        routes = _route_auth(app)
        assert "GET /api/scenarios" in routes  # the walk sees the mounted routers
        assert not [r for r in routes if r.split(" ", 1)[1].startswith("/events")]
        assert client.get("/events/inc-x").status_code == 404


# ── Startup order, Key Vault and PG* wiring ───────────────────────────────────


async def test_key_vault_loads_before_langfuse_and_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    captured: list[Settings] = []

    def configure_auth(_settings: Settings) -> bool:
        order.append("auth")
        return False

    async def load(settings: Settings, _credential: object) -> Settings:
        order.append("kv")
        return settings.model_copy(update={"dd_api_key": SecretStr("kv-dd")})

    def configure_langfuse() -> bool:
        order.append("langfuse")
        return False

    async def init_pool(_settings: Settings, **_kwargs: Any) -> None:
        order.append("pool")

    def wire(_app: FastAPI, settings: Settings, _pool: object, _tasks: object, **_: object) -> None:
        order.append("wire")
        captured.append(settings)

    monkeypatch.setattr(main, "configure_auth", configure_auth)
    monkeypatch.setattr(main, "load_key_vault_secrets", load)
    monkeypatch.setattr(main, "configure_langfuse", configure_langfuse)
    monkeypatch.setattr(main, "init_pool_from_settings", init_pool)
    monkeypatch.setattr(main, "_wire_app", wire)

    async with main.lifespan(FastAPI()):
        pass

    assert order == ["auth", "kv", "langfuse", "pool", "wire"]
    assert captured[0].dd_api_key is not None
    assert captured[0].dd_api_key.get_secret_value() == "kv-dd"


async def test_pg_env_builds_pool_with_entra_token_callable(
    monkeypatch: pytest.MonkeyPatch, boot_env: dict[str, MagicMock]
) -> None:
    monkeypatch.setenv("PGHOST", "sentinel-pg-0375.postgres.database.azure.com")
    monkeypatch.setenv("PGDATABASE", "sentinel")
    monkeypatch.setenv("PGUSER", "sentinel-backend-wi")
    credential = boot_env["credential"].return_value
    credential.get_token.return_value = AccessToken("entra-tok", 4102444800)
    init = AsyncMock(return_value=None)
    monkeypatch.setattr(database, "init_pool", init)
    monkeypatch.setattr(main, "_wire_app", MagicMock())

    async with main.lifespan(FastAPI()):
        call = init.await_args
        assert call is not None
        assert call.args[0] == (
            "postgresql://sentinel-backend-wi@sentinel-pg-0375.postgres.database.azure.com"
            ":5432/sentinel?sslmode=require"
        )
        assert await call.kwargs["password"]() == "entra-tok"
        credential.get_token.assert_called_once_with(POSTGRES_ENTRA_SCOPE)
        boot_env["credential"].assert_called_once_with()
    credential.close.assert_called_once_with()


# ── Teardown ──────────────────────────────────────────────────────────────────


class _RecordingCredential:
    """Stands in for LazyAzureCredential: records close(); any use of get() is a bug here."""

    events: list[str]

    def get(self) -> object:
        raise AssertionError("no Azure call expected")

    def close(self) -> None:
        self.events.append("credential")


def _teardown_harness(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []
    credential_cls = type("Credential", (_RecordingCredential,), {"events": events})
    monkeypatch.setattr(main, "_wire_app", MagicMock())
    monkeypatch.setattr(main, "shutdown_traces", lambda: events.append("traces"))
    monkeypatch.setattr(main, "close_pool", AsyncMock(side_effect=lambda: events.append("pool")))
    monkeypatch.setattr(main, "LazyAzureCredential", credential_cls)
    return events


async def test_teardown_order(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _teardown_harness(monkeypatch)
    async with main.lifespan(FastAPI()):
        events.append("serving")
    assert events == ["serving", "traces", "pool", "credential"]


async def test_teardown_closes_credential_when_close_pool_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _teardown_harness(monkeypatch)
    monkeypatch.setattr(main, "close_pool", AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        async with main.lifespan(FastAPI()):
            pass
    assert "credential" in events


async def test_failed_startup_still_tears_down(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _teardown_harness(monkeypatch)
    monkeypatch.setattr(main, "_wire_app", MagicMock(side_effect=RuntimeError("wire failed")))
    with pytest.raises(RuntimeError, match="wire failed"):
        async with main.lifespan(FastAPI()):
            pass  # pragma: no cover — startup raised before yield
    assert events == ["traces", "pool", "credential"]


async def test_shutdown_waits_for_an_in_flight_incident_before_closing_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan exit is what uvicorn runs on SIGTERM: a finishing incident still has its pool."""
    events = _teardown_harness(monkeypatch)
    monkeypatch.setenv("SENTINEL_SHUTDOWN_GRACE_S", "5")
    started = asyncio.Event()

    async def pipeline() -> None:
        started.set()
        await asyncio.sleep(0.05)
        events.append("stored")

    monkeypatch.setattr(
        main, "_wire_app", lambda _a, _s, _p, tasks, **_: tasks.start("inc-1", pipeline())
    )

    async with main.lifespan(FastAPI()):
        await started.wait()
        events.append("sigterm")

    assert events == ["sigterm", "stored", "traces", "pool", "credential"]


async def test_shutdown_logs_cancelled_incident_before_closing_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _teardown_harness(monkeypatch)
    monkeypatch.setenv("SENTINEL_SHUTDOWN_GRACE_S", "0.05")
    started = asyncio.Event()
    log = MagicMock()
    log.error.side_effect = lambda event, **kw: events.append(f"{event}:{kw['incident_id']}")
    monkeypatch.setattr(tasks_module, "logger", log)

    async def pipeline() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    monkeypatch.setattr(
        main, "_wire_app", lambda _a, _s, _p, tasks, **_: tasks.start("inc-1", pipeline())
    )

    async with main.lifespan(FastAPI()):
        await started.wait()
        events.append("sigterm")

    assert events == [
        "sigterm",
        "incident_cancelled_on_shutdown:inc-1",
        "cancelled",
        "traces",
        "pool",
        "credential",
    ]


def _terminal_run() -> TerminalRun:
    incident_id = str(uuid4())
    request = PipelineInput(
        incident_id=incident_id,
        alert=IncidentAlert(title="t"),
        signal_type="runtime_error",
        correlation_id=str(uuid4()),
    )
    result = PipelineResult(
        incident_id=incident_id,
        status="escalated",
        resolution_type="escalated",
        root_cause="not sure",
        confidence=0.4,
        escalation=EscalationContext(
            hypothesis="not sure", evidence=["e"], confidence=0.4, missing=["x"]
        ),
        reflexion_loops=0,
        tool_calls_used=0,
        tokens_used=0,
    )
    return TerminalRun(
        request=request,
        result=result,
        timeline=(),
        suspect=None,
        raw_hypothesis=False,
        raw_evidence=False,
    )


async def test_shutdown_drains_a_dataset_write_before_the_traces_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _teardown_harness(monkeypatch)
    monkeypatch.setenv("SENTINEL_SHUTDOWN_GRACE_S", "5")
    writes = DatasetWrites()
    monkeypatch.setattr(main, "DATASET_WRITES", writes)

    class Writer:
        async def write(self, run: TerminalRun) -> bool:
            await asyncio.sleep(0.05)
            events.append("item")
            return True

    monkeypatch.setattr(
        main, "_wire_app", lambda _a, _s, _p, _t, **_: writes.start(Writer(), _terminal_run())
    )

    async with main.lifespan(FastAPI()):
        pass

    assert events == ["item", "traces", "pool", "credential"]
    assert len(writes) == 0


async def test_shutdown_cancels_a_hanging_dataset_write_within_the_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _teardown_harness(monkeypatch)
    monkeypatch.setenv("SENTINEL_SHUTDOWN_GRACE_S", "0.2")
    writes = DatasetWrites(timeout_s=60)
    monkeypatch.setattr(main, "DATASET_WRITES", writes)

    class Writer:
        async def write(self, run: TerminalRun) -> bool:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("write_cancelled")
                raise
            return True  # pragma: no cover

    monkeypatch.setattr(
        main, "_wire_app", lambda _a, _s, _p, _t, **_: writes.start(Writer(), _terminal_run())
    )

    lifespan = main.lifespan(FastAPI())
    await lifespan.__aenter__()
    started = time.monotonic()
    await lifespan.__aexit__(None, None, None)
    elapsed = time.monotonic() - started

    assert elapsed < 0.2 + 1.0
    assert events == ["write_cancelled", "traces", "pool", "credential"]
    assert len(writes) == 0


async def test_lifespan_boots_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """No REDIS_HOST (and no database): every Redis-backed feature uses its fallback."""
    monkeypatch.delenv("REDIS_HOST", raising=False)
    wire = MagicMock(return_value=None)
    log = MagicMock()
    monkeypatch.setattr(main, "_wire_app", wire)
    monkeypatch.setattr(main, "logger", log)

    async with main.lifespan(FastAPI()):
        pass

    kwargs = wire.call_args.kwargs
    assert kwargs["redis"] is None and kwargs["stm_mirror"] is None
    assert kwargs["cache"] is None and kwargs["checkpoints"] is None
    startup = [c for c in log.info.call_args_list if c.args == ("sentinel_startup",)]
    assert startup[0].kwargs["redis_configured"] is False
    assert startup[0].kwargs["cache_enabled"] is False
    assert ("sentinel_ready",) in [c.args for c in log.info.call_args_list]
