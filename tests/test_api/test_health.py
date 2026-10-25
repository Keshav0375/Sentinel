"""Tests for sentinel.api.health — GET /health and GET /ready.

Replaces the /health tests formerly in test_incidents.py.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast
from unittest.mock import MagicMock

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinel import main
from sentinel.api.health import (
    ReadinessCheck,
    database_configured,
    db_readiness,
    make_health_router,
    ping_database,
)
from sentinel.config import Settings

# ── Helpers ───────────────────────────────────────────────────────────────────


def _app(readiness: ReadinessCheck | None) -> FastAPI:
    app = FastAPI()
    app.include_router(make_health_router(readiness))
    return app


async def _always_ready() -> bool:
    return True


class _StubConn:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.queries: list[str] = []

    async def fetchval(self, query: str) -> int:
        self.queries.append(query)
        await asyncio.sleep(self.delay_s)
        return 1


class _StubPool:
    def __init__(self, conn: _StubConn | None = None, exc: BaseException | None = None) -> None:
        self.conn = conn or _StubConn()
        self.exc = exc

    async def acquire(self, timeout: float | None = None) -> _StubConn:
        if self.exc is not None:
            raise self.exc
        return self.conn

    async def release(self, conn: _StubConn) -> None:
        return None


def _as_pool(stub: _StubPool) -> asyncpg.Pool:
    return cast(asyncpg.Pool, stub)


def _lifespan_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("PGHOST", "PGDATABASE", "PGUSER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(main, "EmbeddingClient", MagicMock())
    monkeypatch.setattr(main, "install_trace_processors", MagicMock())
    monkeypatch.setattr(main, "apply_sdk_defaults", MagicMock())


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ── /health ───────────────────────────────────────────────────────────────────


def test_health_is_exactly_ok() -> None:
    with TestClient(_app(None)) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_probes_need_no_token() -> None:
    with TestClient(_app(_always_ready)) as client:
        health = client.get("/health")
        ready = client.get("/ready")
    assert health.status_code == 200
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


# ── /ready with no database pool ──────────────────────────────────────────────


def test_ready_not_wired_is_503() -> None:
    with TestClient(_app(None)) as client:
        resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready"}


def test_ready_no_database_configured_is_ready() -> None:
    with TestClient(_app(db_readiness(None, db_configured=False))) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_ready_database_configured_without_pool_is_503() -> None:
    with TestClient(_app(db_readiness(None, db_configured=True))) as client:
        resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready"}


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, False),
        ({"database_url": "  "}, False),
        ({"database_url": "postgresql://u:p@h/db"}, True),
        ({"pghost": "h"}, True),
        ({"pgdatabase": "sentinel"}, True),
        ({"pguser": "u"}, True),
        ({"pguser": " "}, False),
    ],
)
def test_database_configured(fields: dict[str, str], expected: bool) -> None:
    settings = Settings(_env_file=None, **fields)  # type: ignore[arg-type]
    assert database_configured(settings) is expected


def test_database_configured_ignores_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGHOST", "h")
    settings = Settings(_env_file=None, pghost=None)  # type: ignore[call-arg]
    assert database_configured(settings) is False


# ── /ready with a pool ────────────────────────────────────────────────────────


async def test_ping_database_runs_select_1() -> None:
    conn = _StubConn()
    assert await ping_database(_as_pool(_StubPool(conn))) is True
    assert conn.queries == ["SELECT 1"]


async def test_ready_503_when_ping_fails() -> None:
    pool = _as_pool(_StubPool(exc=OSError("refused")))
    resp = await _get(_app(db_readiness(pool, db_configured=True)), "/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready"}


async def test_ready_503_when_ping_times_out() -> None:
    pool = _as_pool(_StubPool(_StubConn(delay_s=5.0)))
    app = _app(db_readiness(pool, db_configured=True, timeout_s=0.05))
    started = time.monotonic()
    resp = await _get(app, "/ready")
    elapsed = time.monotonic() - started
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready"}
    assert elapsed < 1.0


async def test_ready_200_with_database_up(pg_dsn: str) -> None:
    pool: Any = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
    try:
        resp = await _get(_app(db_readiness(pool, db_configured=True)), "/ready")
    finally:
        await pool.close()
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


async def test_ready_503_when_pool_closed(pg_dsn: str) -> None:
    pool: Any = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
    await pool.close()
    resp = await _get(_app(db_readiness(pool, db_configured=True)), "/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready"}


# ── The real lifespan ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("fake_llm", ["1", "0"])
def test_lifespan_ready_without_database_any_fake_mode(
    fake_llm: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lifespan_env(monkeypatch)
    monkeypatch.setenv("SENTINEL_FAKE_LLM", fake_llm)
    with TestClient(main.create_app()) as client:
        first = client.get("/ready")
        second = client.get("/ready")
        health = client.get("/health")
    for resp in (first, second):
        assert resp.status_code == 200
        assert resp.json() == {"status": "ready"}
    assert health.json() == {"status": "ok"}


def test_lifespan_ready_with_database(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _lifespan_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    monkeypatch.setenv("SENTINEL_DB_POOL_MIN", "1")
    monkeypatch.setenv("SENTINEL_DB_POOL_MAX", "2")
    with TestClient(main.create_app()) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_lifespan_pg_env_without_pool_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _lifespan_env(monkeypatch)
    monkeypatch.setenv("PGHOST", "h")  # incomplete libpq config: no pool opens
    with TestClient(main.create_app()) as client:
        resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready"}
