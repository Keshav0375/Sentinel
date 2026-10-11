"""Shared test fixtures — mock memory, mock scenario, test config."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from sentinel.infra.database import is_postgres_dsn

# ── PostgreSQL + pgvector ─────────────────────────────────────────────────────
# Integration tests that need Postgres read DATABASE_URL, e.g. against
#   docker run -d --name sentinel-pg-test -e POSTGRES_PASSWORD=test \
#     -e POSTGRES_DB=sentinel -p 55432:5432 pgvector/pgvector:pg16
#   DATABASE_URL=postgresql://postgres:test@localhost:55432/sentinel
# Without it they skip with a reason; everything else runs as before.

_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _postgres_dsn() -> str | None:
    return _DATABASE_URL if is_postgres_dsn(_DATABASE_URL) else None


def make_alembic_config(dsn: str) -> Config:
    """Alembic config for the repo's migrations, pointed at *dsn*.

    Leaves pytest's logging alone (``configure_logger``) and passes the DSN through
    ``attributes`` so the environment's DATABASE_URL never leaks into the target.
    """
    cfg = Config(str(_ALEMBIC_INI))
    cfg.attributes["database_url"] = dsn
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema() -> Iterator[None]:
    """Run ``alembic upgrade head`` on the test database when DATABASE_URL is Postgres.

    Every pooled connection registers the pgvector codec (needs the extension), and the
    app lifespan exercised by tests/test_api runs against the real §5.3 schema. A no-op
    without a Postgres DSN.
    """
    dsn = _postgres_dsn()
    if dsn is not None:
        command.upgrade(make_alembic_config(dsn), "head")
    yield


@pytest.fixture
def alembic_config() -> Callable[[str], Config]:
    """Factory: an alembic :class:`Config` targeting the given DSN."""
    return make_alembic_config


@pytest.fixture
def pg_dsn() -> str:
    """The Postgres DSN under test; skips the test when none is configured."""
    dsn = _postgres_dsn()
    if dsn is None:
        pytest.skip("DATABASE_URL is not a postgres:// DSN — start pgvector/pgvector:pg16")
    return dsn
