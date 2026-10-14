"""Shared test fixtures — mock memory, mock scenario, test config.

The suite is independent of the caller's shell and of the developer's ``.env``:
``Settings`` never reads ``.env`` here, ``DATABASE_URL`` is unset for the whole session
(a test that needs it sets it to the test database), ``GROQ_API_KEY`` gets a dummy
default, and the LangFuse keys are removed so the client is disabled and nothing is ever
sent. The only database the suite touches is ``TEST_DATABASE_URL``.
"""

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
# Integration tests that need Postgres read TEST_DATABASE_URL — never DATABASE_URL, so
# the app's own database can never be migrated, or have databases created and dropped
# on its server, by a test run. Locally:
#   docker run -d --name sentinel-pg-test -e POSTGRES_PASSWORD=test \
#     -e POSTGRES_DB=sentinel -p 55432:5432 pgvector/pgvector:pg16
#   TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/sentinel
# Without it they skip with a reason — except in CI (CI=true), where they fail.

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
_IN_CI = os.environ.get("CI", "").strip().lower() == "true"
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_LANGFUSE_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")


def pytest_configure(config: pytest.Config) -> None:
    """Before collection: a dummy GROQ_API_KEY (Settings boots without one, but agents
    built from the default groq/ models need one), and no LangFuse keys — a keyless
    langfuse client is a disabled no-op (no network)."""
    os.environ.setdefault("GROQ_API_KEY", "fake-key-for-tests")
    for var in _LANGFUSE_KEYS:
        os.environ.pop(var, None)


def _postgres_dsn() -> str | None:
    return _TEST_DATABASE_URL if is_postgres_dsn(_TEST_DATABASE_URL) else None


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
def _isolated_environment() -> Iterator[None]:
    """No ``.env`` for Settings, no ``DATABASE_URL`` and no LangFuse keys for the session.

    The keys are removed again here, after collection: importing litellm loads the
    developer's ``.env`` into ``os.environ``.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(Settings.model_config, "env_file", None)
        mp.delenv("DATABASE_URL", raising=False)
        for var in _LANGFUSE_KEYS:
            mp.delenv(var, raising=False)
        yield


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema(_isolated_environment: None) -> Iterator[None]:
    """Run ``alembic upgrade head`` on the test database when TEST_DATABASE_URL is set.

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
    """The Postgres DSN under test (TEST_DATABASE_URL).

    Skips the test when none is configured; in CI a missing test database is a failure,
    so the database tests can never silently not run there.
    """
    dsn = _postgres_dsn()
    if dsn is None:
        reason = "TEST_DATABASE_URL is not a postgres:// DSN — start pgvector/pgvector:pg16"
        if _IN_CI:
            pytest.fail(f"{reason} (CI=true: the database tests must run)", pytrace=False)
        pytest.skip(reason)
    return dsn
