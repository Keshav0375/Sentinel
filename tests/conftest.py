"""Shared test fixtures — mock memory, mock scenario, test config."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from sentinel.config import Settings
from sentinel.infra.database import is_postgres_dsn
from sentinel.memory.embeddings import EmbeddingClient

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


@cache
def real_embedding_client() -> EmbeddingClient:
    """The configured sentence-transformers model, loaded once per test session.

    Only Postgres-backed tests call it: ``002_seed_services`` embeds its row with it, and
    loading the model per migration run would cost seconds each time.
    """
    model = Settings.model_fields["sentinel_embedding_model"].default
    return EmbeddingClient.from_model_name(model)


def make_alembic_config(dsn: str, embedding_client: EmbeddingClient | None = None) -> Config:
    """Alembic config for the repo's migrations, pointed at *dsn*.

    Leaves pytest's logging alone (``configure_logger``) and passes the DSN through
    ``attributes`` so the environment's DATABASE_URL never leaks into the target.
    *embedding_client*, when given, is the client ``002_seed_services`` embeds with;
    without one the migration loads the configured model itself.
    """
    cfg = Config(str(_ALEMBIC_INI))
    cfg.attributes["database_url"] = dsn
    cfg.attributes["configure_logger"] = False
    if embedding_client is not None:
        cfg.attributes["embedding_client"] = embedding_client
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
        command.upgrade(make_alembic_config(dsn, real_embedding_client()), "head")
    yield


@pytest.fixture
def alembic_config() -> Callable[..., Config]:
    """Factory: an alembic :class:`Config` targeting the given DSN."""
    return make_alembic_config


@pytest.fixture(scope="session")
def seed_embedding_client() -> EmbeddingClient:
    """The real embedding client (see :func:`real_embedding_client`); loads the model."""
    return real_embedding_client()


@pytest.fixture
def pg_dsn() -> str:
    """The Postgres DSN under test; skips the test when none is configured."""
    dsn = _postgres_dsn()
    if dsn is None:
        pytest.skip("DATABASE_URL is not a postgres:// DSN — start pgvector/pgvector:pg16")
    return dsn
