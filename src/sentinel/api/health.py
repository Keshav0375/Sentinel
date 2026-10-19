"""Liveness and readiness probes — GET /health, GET /ready (backend.md §3.5).

The only unauthenticated routes (decision 2026-10-19); K8s probes carry no token.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from sentinel.config import Settings, get_settings
from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.database import acquire
from sentinel.infra.logging import get_logger

logger = get_logger("sentinel.api.health")

READY_PING_TIMEOUT_S: float = 2.0

# libpq environment variables that configure a database without DATABASE_URL (§8.2).
_PG_ENV_VARS: tuple[str, ...] = ("PGHOST", "PGDATABASE", "PGUSER")

ReadinessCheck = Callable[[], Awaitable[bool]]


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    """Response body for GET /ready."""

    status: Literal["ready", "not_ready"]


def database_configured(settings: Settings, environ: Mapping[str, str] | None = None) -> bool:
    """Say whether a Postgres database is configured for this process.

    Args:
        settings: Loaded settings; a non-blank ``database_url`` counts as configured.
        environ: Environment to inspect for ``PGHOST``/``PGDATABASE``/``PGUSER``;
            defaults to ``os.environ``.

    Returns:
        True when ``database_url`` or any libpq variable is non-blank.
    """
    env = os.environ if environ is None else environ
    return bool(settings.database_url.strip()) or any(env.get(v, "").strip() for v in _PG_ENV_VARS)


async def ping_database(pool: asyncpg.Pool, *, timeout_s: float = READY_PING_TIMEOUT_S) -> bool:
    """Run ``SELECT 1`` on a pooled connection within *timeout_s* seconds.

    Args:
        pool: The pool to borrow a connection from.
        timeout_s: Bound for the whole ping, acquire included.

    Returns:
        True when the query completed; False on any database failure or timeout.
    """
    try:
        async with asyncio.timeout(timeout_s):
            async with acquire(pool, timeout=timeout_s) as conn:
                await conn.fetchval("SELECT 1")
    except (SentinelMemoryError, TimeoutError, OSError) as exc:
        logger.warning("ready_db_ping_failed", error=type(exc).__name__)
        return False
    return True


def db_readiness(
    pool: asyncpg.Pool | None,
    *,
    db_configured: bool | None = None,
    timeout_s: float = READY_PING_TIMEOUT_S,
) -> ReadinessCheck:
    """Build the readiness check for a (possibly absent) database pool.

    Args:
        pool: The asyncpg pool, or None when none was opened.
        db_configured: Whether a database is configured; resolved once, now, from
            settings and the environment when None.
        timeout_s: Bound for the ping.

    Returns:
        A coroutine function: True when ready. With a pool it pings the database;
        without one it is ready only when no database is configured.
    """
    configured = database_configured(get_settings()) if db_configured is None else db_configured

    async def check() -> bool:
        if pool is None:
            if configured:
                logger.warning("ready_db_pool_missing")
            return not configured
        return await ping_database(pool, timeout_s=timeout_s)

    return check


def make_health_router(readiness: ReadinessCheck | None = None) -> APIRouter:
    """Create the probe router.

    Args:
        readiness: Readiness check behind ``GET /ready``. ``None`` means no check is
            wired, which reports not ready.

    Returns:
        ``APIRouter`` with ``GET /health`` and ``GET /ready`` registered.
    """
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:  # pyright: ignore[reportUnusedFunction]
        """Return 200 while the process is serving."""
        return HealthResponse()

    @router.get(
        "/ready",
        response_model=ReadyResponse,
        responses={503: {"model": ReadyResponse}},
    )
    async def ready_check(response: Response) -> ReadyResponse:  # pyright: ignore[reportUnusedFunction]
        """Return 200 when the service can take traffic, 503 otherwise."""
        if readiness is not None and await readiness():
            return ReadyResponse(status="ready")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="not_ready")

    return router
