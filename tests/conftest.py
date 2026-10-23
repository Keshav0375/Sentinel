"""Shared test fixtures — mock memory, mock scenario, test config.

The suite is independent of the caller's shell and of the developer's ``.env``:
``Settings`` never reads ``.env`` here, ``DATABASE_URL`` is unset for the whole session
(a test that needs it sets it to the test database), the LLM provider keys and
``SENTINEL_PRIMARY_PROVIDER`` are removed so no test can reach a real model, the LangFuse
keys are removed so the client is disabled and nothing is ever sent, and the Datadog
settings are removed so ``Settings`` falls back to its defaults (a test that needs keys
passes fakes). The Azure runtime variables (Key Vault, PG*, route-auth tenant) are removed
too, so no test reaches Azure or a developer's tenant. The only database the suite touches is
``TEST_DATABASE_URL``. These variables are removed again before every test, so a test file
run on its own (which may import litellm, and so load ``.env``, only mid-session) is as
isolated as the full suite.

openai-agents' default trace processor (which uploads spans to OpenAI) is dropped at
start-up, as the app does, and any HTTP request to api.openai.com fails the test.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path
from typing import Any

import httpx

# Imported first on purpose: litellm loads a developer .env into os.environ on its first
# import. Doing that here, before pytest_configure and the fixtures below scrub the
# environment, keeps a lazy import inside a test from re-adding DATABASE_URL or keys.
import litellm  # noqa: F401  # pyright: ignore[reportUnusedImport]
import pytest
from agents import set_trace_processors
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
# Removed with the LangFuse keys: the suite never holds a real Datadog key or site.
_DATADOG_VARS = ("DD_API_KEY", "DD_APP_KEY", "DD_SITE")
# Removed too: the suite never holds a real LLM key, and the §4.7 default provider applies.
_LLM_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "SENTINEL_PRIMARY_PROVIDER",
    "SENTINEL_FAKE_LLM",
)
# Removed too: no test reaches Azure or a developer's tenant, and the app's route auth is
# unconfigured (401) unless a test configures it.
_RUNTIME_VARS = (
    "KEY_VAULT_URL",
    "PGHOST",
    "PGDATABASE",
    "PGUSER",
    "SENTINEL_DEV_OPEN_DASHBOARD",
    "SENTINEL_API_TENANT_ID",
    "SENTINEL_API_AUDIENCE",
)


def pytest_configure(config: pytest.Config) -> None:
    """Before collection: no LLM keys (a test that needs one passes a fake to Settings)
    and no LangFuse keys — a keyless langfuse client is a disabled no-op (no network)."""
    for var in (*_LANGFUSE_KEYS, *_DATADOG_VARS, *_LLM_VARS, *_RUNTIME_VARS):
        os.environ.pop(var, None)
    # Drop the SDK's default OpenAI trace exporter for the whole run, as the app does at
    # startup (infra/tracing.install_trace_processors); a test that needs processors sets
    # its own.
    set_trace_processors([])


_OPENAI_HOST = "api.openai.com"


class OpenAIRequestBlocked(AssertionError):
    """A test tried to reach api.openai.com (model or trace upload)."""


@pytest.fixture(scope="session", autouse=True)
def _no_openai_requests() -> Iterator[None]:
    """Fail any httpx request to api.openai.com — the trace exporter included."""
    real_send = httpx.Client.send
    real_async_send = httpx.AsyncClient.send

    def send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if request.url.host == _OPENAI_HOST:
            raise OpenAIRequestBlocked(f"request to {request.url} during tests")
        return real_send(self, request, **kwargs)

    async def async_send(
        self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        if request.url.host == _OPENAI_HOST:
            raise OpenAIRequestBlocked(f"request to {request.url} during tests")
        return await real_async_send(self, request, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx.Client, "send", send)
        mp.setattr(httpx.AsyncClient, "send", async_send)
        yield


@pytest.fixture(autouse=True)
def _scrubbed_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before every test: no DATABASE_URL, LLM, LangFuse, Datadog or Azure-runtime variable.

    litellm loads a developer ``.env`` into ``os.environ`` on first import, which for a
    test file run alone can be inside a test (e.g. a lazy ``import sentinel.main``),
    after the session fixture ran. A test that needs one of these sets it itself.
    """
    for var in ("DATABASE_URL", *_LANGFUSE_KEYS, *_DATADOG_VARS, *_LLM_VARS, *_RUNTIME_VARS):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_llm_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model-call retries keep their count but not their 1s/2s waits (tests run fast).

    A test of the waits themselves sets ``router.LLM_RETRY_BACKOFF_S`` back.
    """
    from sentinel.providers import router  # noqa: PLC0415 — after litellm's .env load

    monkeypatch.setattr(router, "LLM_RETRY_BACKOFF_S", (0.0, 0.0))


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
    """No ``.env`` for Settings, no ``DATABASE_URL``, LLM or LangFuse keys for the session.

    The keys are removed again here, after collection: importing litellm loads the
    developer's ``.env`` into ``os.environ``.
    """
    # The LLM keys are dropped for good, not restored at teardown: the openai-agents trace
    # exporter flushes at interpreter exit and would otherwise post the session's traces
    # to api.openai.com with whatever OPENAI_API_KEY litellm loaded from a developer .env.
    for var in _LLM_VARS:
        os.environ.pop(var, None)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(Settings.model_config, "env_file", None)
        mp.delenv("DATABASE_URL", raising=False)
        for var in (*_LANGFUSE_KEYS, *_DATADOG_VARS):
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
