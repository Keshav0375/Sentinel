"""Shared test fixtures — mock memory, mock scenario, test config."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import asyncpg
import pytest

from sentinel.infra.database import is_postgres_dsn

# ── PostgreSQL + pgvector ─────────────────────────────────────────────────────
# Integration tests that need Postgres read DATABASE_URL, e.g. against
#   docker run -d --name sentinel-pg-test -e POSTGRES_PASSWORD=test \
#     -e POSTGRES_DB=sentinel -p 55432:5432 pgvector/pgvector:pg16
#   DATABASE_URL=postgresql://postgres:test@localhost:55432/sentinel
# Without it they skip with a reason; everything else runs as before.

_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def _postgres_dsn() -> str | None:
    return _DATABASE_URL if is_postgres_dsn(_DATABASE_URL) else None


async def _create_vector_extension(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def _pgvector_extension() -> Iterator[None]:
    """Ensure the ``vector`` extension exists when a Postgres DATABASE_URL is set.

    Every pooled connection registers the pgvector codec, which needs the extension —
    including the app lifespan exercised by tests/test_api. Stands in for alembic
    migration 001 until it lands; a no-op without a Postgres DSN.
    """
    dsn = _postgres_dsn()
    if dsn is not None:
        asyncio.run(_create_vector_extension(dsn))
    yield


@pytest.fixture
def pg_dsn() -> str:
    """The Postgres DSN under test; skips the test when none is configured."""
    dsn = _postgres_dsn()
    if dsn is None:
        pytest.skip("DATABASE_URL is not a postgres:// DSN — start pgvector/pgvector:pg16")
    return dsn
