"""Liveness and readiness probes — GET /health, GET /ready (backend.md §3.5).

The only unauthenticated routes (decision 2026-10-19); K8s probes carry no token.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from sentinel.config import Settings
from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.database import acquire
from sentinel.infra.logging import get_logger

logger = get_logger("sentinel.api.health")

READY_PING_TIMEOUT_S: float = 2.0

ReadinessCheck = Callable[[], Awaitable[bool]]


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    """Response body for GET /ready."""

    status: Literal["ready", "not_ready"]


def database_configured(settings: Settings) -> bool:
    """Say whether a Postgres database is configured for this process.

    Read from the loaded Settings only; never reads the process environment.

    Args:
        settings: Loaded settings; a non-blank ``database_url`` or any set libpq field
            (``pghost``, ``pgdatabase``, ``pguser``; Settings maps blank to None) counts.

    Returns:
        True when ``database_url`` or any of those fields is set.
    """
    return bool(settings.database_url.strip()) or any(
        v is not None for v in (settings.pghost, settings.pgdatabase, settings.pguser)
    )


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
    db_configured: bool,
    timeout_s: float = READY_PING_TIMEOUT_S,
) -> ReadinessCheck:
    """Build the readiness check for a (possibly absent) database pool.

    Args:
        pool: The asyncpg pool, or None when none was opened.
        db_configured: Whether a database is configured (see :func:`database_configured`).
        timeout_s: Bound for the ping.

    Returns:
        A coroutine function: True when ready. With a pool it pings the database;
        without one it is ready only when no database is configured.
    """

    async def check() -> bool:
        if pool is None:
            if db_configured:
                logger.warning("ready_db_pool_missing")
            return not db_configured
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
