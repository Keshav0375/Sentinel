"""Tests for sentinel.infra.database — the asyncpg + pgvector pool.

Tests taking ``pg_dsn`` run against a real pgvector container (see tests/conftest.py)
and skip with a reason when TEST_DATABASE_URL is not set (fail in CI).
"""

from __future__ import annotations

import asyncio
import traceback
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from pgvector import Vector

from sentinel.config import Settings
from sentinel.exceptions import SentinelError, SentinelMemoryError
from sentinel.infra import database
from sentinel.infra.database import (
    acquire,
    close_pool,
    create_pool,
    get_pool,
    init_pool,
    init_pool_from_settings,
    is_postgres_dsn,
)

# ── Helpers / fixtures ────────────────────────────────────────────────────────


def _settings(**kwargs: object) -> Settings:
    return Settings(**kwargs)  # type: ignore[arg-type]


def _without_password(dsn: str) -> tuple[str, str]:
    """Split *dsn* into (dsn without password, password)."""
    parts = urlsplit(dsn)
    netloc = f"{parts.username}@{parts.hostname}:{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc)), parts.password or ""


async def _backend_count(dsn: str) -> int:
    """Server-side connections to this database, excluding the counting one."""
    conn = await asyncpg.connect(dsn)
    try:
        count = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
    finally:
        await conn.close()
    return int(count)


async def _wait_for_backend_count(dsn: str, expected: int) -> int:
    """pg_stat_activity lags a closed socket slightly — poll briefly."""
    count = await _backend_count(dsn)
    for _ in range(50):
        if count == expected:
            break
        await asyncio.sleep(0.05)
        count = await _backend_count(dsn)
    return count


@pytest.fixture
async def pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(pg_dsn, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture(autouse=True)
async def _reset_module_pool() -> AsyncIterator[None]:
    """Never leak the process-wide pool between tests."""
    await close_pool()
    yield
    await close_pool()


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_memory_error_is_sentinel_error_not_builtin() -> None:
    assert issubclass(SentinelMemoryError, SentinelError)
    assert not issubclass(SentinelMemoryError, MemoryError)


# ── DSN detection + settings wiring (no DB needed) ────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@localhost:5432/db", True),
        ("postgres://u@db.example.com/sentinel", True),
        ("  POSTGRESQL://u@h/db ", True),
        ("sqlite:///./data/sentinel.db", False),
        ("postgresql+asyncpg://u@h/db", False),
        ("", False),
    ],
)
def test_is_postgres_dsn(url: str, expected: bool) -> None:
    assert is_postgres_dsn(url) is expected


async def test_init_from_settings_without_database_url_creates_no_pool() -> None:
    assert await init_pool_from_settings(_settings(database_url="")) is None
    with pytest.raises(SentinelMemoryError, match="not initialised"):
        get_pool()


async def test_init_from_settings_ignores_non_postgres_url() -> None:
    settings = _settings(database_url="sqlite:///./data/sentinel.db")
    assert await init_pool_from_settings(settings) is None
    with pytest.raises(SentinelMemoryError):
        get_pool()


async def test_acquire_without_pool_raises_memory_error() -> None:
    with pytest.raises(SentinelMemoryError, match="not initialised"):
        async with acquire():
            pass


async def test_acquire_timeout_wrapped_with_readable_message() -> None:
    """TimeoutError() has an empty str() — the wrapped message must still name it."""

    class _TimingOutPool:
        async def acquire(self, *, timeout: float | None = None) -> None:
            raise TimeoutError

    with pytest.raises(SentinelMemoryError, match="TimeoutError") as excinfo:
        async with acquire(_TimingOutPool()):  # type: ignore[arg-type]
            pass
    assert isinstance(excinfo.value.__cause__, TimeoutError)


async def test_close_pool_without_pool_is_noop() -> None:
    await close_pool()
    await close_pool()


# ── Pool lifecycle (pgvector container) ───────────────────────────────────────


async def test_acquire_select_one(pool: asyncpg.Pool) -> None:
    async with acquire(pool) as conn:
        assert await conn.fetchval("SELECT 1") == 1


async def test_acquire_takes_and_release_returns_connection(pool: asyncpg.Pool) -> None:
    idle_before = pool.get_idle_size()
    async with acquire(pool) as conn:
        await conn.fetchval("SELECT 1")
        assert pool.get_idle_size() == idle_before - 1
    assert pool.get_idle_size() == idle_before


async def test_connection_released_when_block_raises(pool: asyncpg.Pool) -> None:
    idle_before = pool.get_idle_size()
    with pytest.raises(RuntimeError):
        async with acquire(pool):
            raise RuntimeError("caller bug")
    assert pool.get_idle_size() == idle_before


async def test_vector_round_trip(pool: asyncpg.Pool) -> None:
    async with acquire(pool) as conn:
        await conn.execute("CREATE TEMP TABLE vec_rt (embedding vector(3))")
        await conn.execute("INSERT INTO vec_rt VALUES ($1)", [1.0, 2.5, -3.0])
        value = await conn.fetchval("SELECT embedding FROM vec_rt")
    assert isinstance(value, Vector)
    assert value.to_list() == [1.0, 2.5, -3.0]


async def test_vector_codec_registered_on_every_connection(pool: asyncpg.Pool) -> None:
    """Hold all max_size connections at once — each must decode ``vector``."""

    async def decode_on_own_connection(ready: asyncio.Barrier) -> list[float]:
        async with acquire(pool) as conn:
            await ready.wait()  # forces max_size distinct connections
            value = await conn.fetchval("SELECT '[0.5,1,2]'::vector(3)")
        assert isinstance(value, Vector)
        return value.to_list()

    size = pool.get_max_size()
    barrier = asyncio.Barrier(size)
    results = await asyncio.gather(*(decode_on_own_connection(barrier) for _ in range(size)))
    assert results == [[0.5, 1.0, 2.0]] * size
    assert pool.get_size() == size


async def test_pool_bounds_honoured_from_config(pg_dsn: str) -> None:
    settings = _settings(database_url=pg_dsn, sentinel_db_pool_min=1, sentinel_db_pool_max=4)
    created = await init_pool_from_settings(settings)
    assert created is not None
    assert get_pool() is created
    assert created.get_min_size() == 1
    assert created.get_max_size() == 4
    assert created.get_size() == 1  # min_size opened eagerly


async def test_default_bounds_match_architecture(pg_dsn: str) -> None:
    created = await init_pool_from_settings(_settings(database_url=pg_dsn))
    assert created is not None
    assert (created.get_min_size(), created.get_max_size()) == (2, 10)


async def test_init_pool_twice_raises(pg_dsn: str) -> None:
    await init_pool(pg_dsn, min_size=1, max_size=1)
    with pytest.raises(SentinelMemoryError, match="already initialised"):
        await init_pool(pg_dsn, min_size=1, max_size=1)


async def test_module_pool_acquire_and_close_leaks_nothing(pg_dsn: str) -> None:
    baseline = await _backend_count(pg_dsn)
    await init_pool(pg_dsn, min_size=2, max_size=3)
    async with acquire() as conn:
        assert await conn.fetchval("SELECT 1") == 1
    assert await _backend_count(pg_dsn) >= baseline + 2

    await close_pool()

    assert database._pool is None  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(SentinelMemoryError):
        get_pool()
    assert await _wait_for_backend_count(pg_dsn, baseline) == baseline


async def test_password_callable_is_used(pg_dsn: str) -> None:
    """The Entra-token hook: an async callable supplies the password per connection."""
    bare_dsn, secret = _without_password(pg_dsn)
    calls = 0

    async def token() -> str:
        nonlocal calls
        calls += 1
        return secret

    p = await create_pool(bare_dsn, min_size=2, max_size=2, password=token)
    try:
        async with acquire(p) as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await p.close()
    assert calls == 2  # once per connection


async def test_sync_password_callable_is_used(pg_dsn: str) -> None:
    """A plain (sync) callable works too — asyncpg accepts both."""
    bare_dsn, secret = _without_password(pg_dsn)
    calls: list[int] = []

    def token() -> str:
        calls.append(1)
        return secret

    p = await create_pool(bare_dsn, min_size=1, max_size=1, password=token)
    try:
        async with acquire(p) as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await p.close()
    assert len(calls) == 1


class _TokenError(Exception):
    """Stands in for an Entra token failure (azure-identity's own exception types)."""


def _raising_provider(kind: str) -> Callable[[], Any]:
    if kind == "sync":

        def sync_token() -> str:
            raise _TokenError("token endpoint unreachable")

        return sync_token

    async def async_token() -> str:
        raise _TokenError("token endpoint unreachable")

    return async_token


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_password_callable_error_wrapped_in_create_pool(pg_dsn: str, kind: str) -> None:
    bare_dsn, _ = _without_password(pg_dsn)
    with pytest.raises(SentinelMemoryError, match=r"password provider failed \(_TokenError\)"):
        await create_pool(bare_dsn, min_size=1, max_size=1, password=_raising_provider(kind))


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_password_callable_error_wrapped_on_lazy_acquire(pg_dsn: str, kind: str) -> None:
    """min_size=0: no connection at creation, so the provider first runs inside acquire()."""
    bare_dsn, _ = _without_password(pg_dsn)
    p = await create_pool(bare_dsn, min_size=0, max_size=1, password=_raising_provider(kind))
    try:
        with pytest.raises(SentinelMemoryError, match="password provider failed") as excinfo:
            async with acquire(p):
                pytest.fail("acquire should have failed")
        assert isinstance(excinfo.value.__cause__, _TokenError)
    finally:
        await p.close()


async def test_missing_vector_extension_is_a_clear_error(pg_dsn: str) -> None:
    name = f"sentinel_novec_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        try:
            dsn = urlunsplit(urlsplit(pg_dsn)._replace(path=f"/{name}"))
            with pytest.raises(SentinelMemoryError, match="vector extension is missing"):
                await create_pool(dsn, min_size=1, max_size=1)
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


# ── Driver errors surface as SentinelMemoryError ──────────────────────────────


async def test_query_error_wrapped(pool: asyncpg.Pool) -> None:
    with pytest.raises(SentinelMemoryError) as excinfo:
        async with acquire(pool) as conn:
            await conn.fetch("SELECT * FROM table_that_does_not_exist")
    assert isinstance(excinfo.value.__cause__, asyncpg.UndefinedTableError)
    # The failed query still returned its connection.
    assert pool.get_idle_size() == pool.get_size()


async def test_bad_password_wrapped(pg_dsn: str) -> None:
    bare_dsn, _ = _without_password(pg_dsn)
    with pytest.raises(SentinelMemoryError) as excinfo:
        await create_pool(bare_dsn, min_size=1, max_size=1, password="wrong-password")
    assert isinstance(excinfo.value.__cause__, asyncpg.InvalidPasswordError)


async def test_unreachable_server_wrapped(pg_dsn: str) -> None:
    parts = urlsplit(pg_dsn)
    dead = urlunsplit(parts._replace(netloc=f"{parts.username}:x@127.0.0.1:1"))
    with pytest.raises(SentinelMemoryError) as excinfo:
        await create_pool(dead, min_size=1, max_size=1)
    assert isinstance(excinfo.value.__cause__, OSError)


# ── DSN text never reaches errors, logs or tracebacks ─────────────────────────

_SECRET = "S3cretTail"

# Each password holds a reserved character, so the DSN does not split where a reader
# expects: asyncpg's own error then quotes the password tail ("invalid literal for
# int(): 'S3cretTail@localhost:1'"), and urlsplit's .port raises on it.
_LEAKY_DSNS = [
    f"postgresql://user:pa@ss:{_SECRET}@localhost:1/db",
    f"postgresql://user:{_SECRET}@localhost:notaport/db",
    f"postgresql://user:pa/ss{_SECRET}@localhost:1/db",
]


def _assert_no_secret(exc: BaseException, captured: str) -> None:
    assert _SECRET not in str(exc)
    assert _SECRET not in "".join(traceback.format_exception(exc))
    assert _SECRET not in captured


@pytest.mark.parametrize("dsn", _LEAKY_DSNS)
async def test_unparseable_dsn_never_leaks_from_create_pool(
    dsn: str, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(SentinelMemoryError, match="could not create database pool") as excinfo:
        await create_pool(dsn, min_size=1, max_size=1)
    out, err = capfd.readouterr()
    assert "db_pool_create_failed" in out + err  # the failure is logged…
    _assert_no_secret(excinfo.value, out + err + caplog.text)  # …without the password


async def test_unparseable_dsn_never_leaks_from_lazy_acquire(
    capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """min_size=0 defers DSN parsing to the first acquire()."""
    p = await create_pool(_LEAKY_DSNS[0], min_size=0, max_size=1)
    try:
        with pytest.raises(SentinelMemoryError, match="could not acquire") as excinfo:
            async with acquire(p):
                pytest.fail("acquire should have failed")
    finally:
        await p.close()
    out, err = capfd.readouterr()
    assert "db_acquire_failed" in out + err
    _assert_no_secret(excinfo.value, out + err + caplog.text)


async def test_invalid_bounds_wrapped(pg_dsn: str) -> None:
    with pytest.raises(SentinelMemoryError):
        await create_pool(pg_dsn, min_size=5, max_size=2)
