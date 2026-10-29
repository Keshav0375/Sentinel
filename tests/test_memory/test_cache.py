"""Tests for sentinel.memory.cache — keys, the Postgres store, the Redis front, stats.

Tests taking ``pool`` run against a throwaway database migrated to head (TEST_DATABASE_URL,
see tests/conftest.py) and skip without it (fail in CI). Redis is the local stub in
tests/redis_stub.py.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, JsonValue
from redis_stub import StubRedis, stub_handle

from sentinel.config import Settings
from sentinel.infra.database import create_pool
from sentinel.memory import cache as cache_module
from sentinel.memory.cache import (
    REDIS_CACHE_PREFIX,
    CacheStats,
    PostgresCache,
    RedisFrontCache,
    build_cache,
    cache_key,
    record_cache_lookup,
    track_cache_stats,
)
from sentinel.memory.embeddings import EmbeddingClient

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


# ── Keys ──────────────────────────────────────────────────────────────────────


class _Args(BaseModel):
    service: str
    minutes: int


def test_cache_key_is_canonical() -> None:
    a = cache_key("tool", {"service": "s", "window_minutes": 60, "include": ["a", "b"]})
    b = cache_key("tool", {"include": ["a", "b"], "window_minutes": 60, "service": "s"})
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)
    assert cache_key("llm", {"service": "s", "window_minutes": 60, "include": ["a", "b"]}) != a
    assert cache_key("tool", {"service": "s", "window_minutes": 61, "include": ["a", "b"]}) != a
    # Models in a payload are dumped canonically, not by repr.
    assert cache_key("llm", {"x": _Args(service="s", minutes=1)}) == cache_key(
        "llm", {"x": {"minutes": 1, "service": "s"}}
    )


# ── Postgres ──────────────────────────────────────────────────────────────────


def _with_database(dsn: str, name: str) -> str:
    return urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))


def _execute(dsn: str, sql: str) -> None:
    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_go())


@pytest.fixture(scope="module")
def _module_db() -> Iterator[dict[str, str]]:
    holder: dict[str, str] = {}
    yield holder
    if holder:
        _execute(holder["server"], f'DROP DATABASE IF EXISTS "{holder["name"]}" WITH (FORCE)')


@pytest.fixture
def cache_dsn(
    pg_dsn: str,
    _module_db: dict[str, str],
    alembic_config: Callable[..., Config],
    seed_embedding_client: EmbeddingClient,
) -> str:
    if not _module_db:
        name = f"sentinel_cache_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"], seed_embedding_client), "head")
    _execute(_module_db["dsn"], "DELETE FROM cache_entries")
    return _module_db["dsn"]


@pytest.fixture
async def pool(cache_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(cache_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def test_postgres_cache_miss_set_hit_counts(pool: asyncpg.Pool) -> None:
    cache = PostgresCache(pool)
    key = cache_key("tool", {"q": 1})
    value: JsonValue = {"sources": {"synthetics": "ok"}, "n": [1, 2]}

    assert await cache.get(key, "tool") is None
    await cache.set(key, "tool", value, 300)
    assert await cache.get(key, "tool") == value

    row = await pool.fetchrow("SELECT kind, hit_count, expires_at FROM cache_entries")
    assert row is not None
    assert row["kind"] == "tool" and row["hit_count"] == 1
    remaining = (row["expires_at"] - datetime.now(UTC)).total_seconds()
    assert 250 < remaining <= 300

    # A set over an existing key replaces its value and expiry.
    await cache.set(key, "tool", {"n": 2}, 60)
    assert await cache.get(key, "tool") == {"n": 2}


async def test_postgres_cache_expiry(pool: asyncpg.Pool) -> None:
    cache = PostgresCache(pool)
    key = cache_key("llm", {"q": 2})
    await cache.set(key, "llm", {"output": []}, 1)
    await cache.set(cache_key("llm", {"q": 3}), "llm", {"output": []}, 300)
    await pool.execute(
        "UPDATE cache_entries SET expires_at = now() - interval '1 second' WHERE key = $1", key
    )

    assert await cache.get(key, "llm") is None
    assert await cache.purge_expired() == 1
    assert await pool.fetchval("SELECT count(*) FROM cache_entries") == 1


# ── Redis front ───────────────────────────────────────────────────────────────


class _Backing:
    """A PostgresCache stand-in: one in-memory map, call counts."""

    def __init__(self, entries: dict[str, tuple[JsonValue, datetime]] | None = None) -> None:
        self.entries = dict(entries or {})
        self.gets = 0
        self.sets: list[tuple[str, int]] = []
        self.hang = False

    async def get_entry(self, key: str) -> tuple[JsonValue, datetime] | None:
        self.gets += 1
        if self.hang:
            await asyncio.Event().wait()
        return self.entries.get(key)

    async def set(self, key: str, kind: str, value: JsonValue, ttl_s: int) -> None:
        self.sets.append((key, ttl_s))
        self.entries[key] = (value, NOW + timedelta(seconds=ttl_s))


def _front(stub: StubRedis, backing: _Backing | None, **kwargs: Any) -> RedisFrontCache:
    return RedisFrontCache(
        stub_handle(stub),
        backing,  # type: ignore[arg-type]
        clock=lambda: NOW,
        **kwargs,
    )


async def test_redis_front_read_through() -> None:
    stub = StubRedis()
    backing = _Backing({"k": ({"v": 1}, NOW + timedelta(seconds=120))})
    front = _front(stub, backing)

    assert await front.get("k", "tool") == {"v": 1}
    assert backing.gets == 1
    assert stub.ttls[REDIS_CACHE_PREFIX + "k"] == 120  # the backing entry's remaining TTL

    assert await front.get("k", "tool") == {"v": 1}
    assert backing.gets == 1  # served from Redis

    await front.set("n", "llm", {"v": 2}, 86400)
    assert backing.sets == [("n", 86400)]
    assert stub.ttls[REDIS_CACHE_PREFIX + "n"] == 86400
    assert await front.get("n", "llm") == {"v": 2}


async def test_redis_front_without_backing() -> None:
    stub = StubRedis()
    front = _front(stub, None)
    assert await front.get("k", "tool") is None
    await front.set("k", "tool", [1], 300)
    assert await front.get("k", "tool") == [1]


async def test_redis_front_falls_back_when_redis_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(cache_module, "logger", log)
    stub = StubRedis()
    stub.fail = True
    backing = _Backing({"k": ({"v": 1}, NOW + timedelta(seconds=60))})
    front = _front(stub, backing)

    assert await front.get("k", "tool") == {"v": 1}
    await front.set("x", "tool", {"v": 3}, 300)  # never raises; the backing still gets it

    assert backing.sets == [("x", 300)]
    ops = [c.kwargs["op"] for c in log.warning.call_args_list if c.args == ("cache_unavailable",)]
    assert ops == ["redis_get", "redis_fill", "redis_set"]
    for call in log.warning.call_args_list:
        assert set(call.kwargs) == {"op", "kind", "error_type"}  # never a key or a value
        assert call.kwargs["error_type"] == "ConnectionError"


async def test_cache_ops_never_raise_and_time_out() -> None:
    backing = _Backing()
    backing.hang = True
    stub = StubRedis()
    front = _front(stub, backing)
    loop = asyncio.get_running_loop()

    # The backing hangs: the front's lookup falls through it within its bound.
    started = loop.time()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(front.get("k", "tool"), 0.2)
    assert loop.time() - started < 1.0

    # A PostgresCache whose pool hangs: get returns None within op_timeout_s.
    class _HangingPool:
        async def acquire(self, *, timeout: float | None = None) -> Any:
            await asyncio.Event().wait()

        async def release(self, conn: Any) -> None:
            pass

    pg = PostgresCache(_HangingPool(), op_timeout_s=0.05)  # type: ignore[arg-type]
    started = loop.time()
    assert await pg.get("k", "tool") is None
    await pg.set("k", "tool", 1, 300)
    assert await pg.purge_expired() == 0
    assert loop.time() - started < 1.0


async def test_redis_front_bounds_a_hanging_backing_with_its_own_timeout() -> None:
    class _SlowPool:
        async def acquire(self, *, timeout: float | None = None) -> Any:
            await asyncio.Event().wait()

        async def release(self, conn: Any) -> None:
            pass

    stub = StubRedis()
    front = RedisFrontCache(
        stub_handle(stub),
        PostgresCache(_SlowPool(), op_timeout_s=0.05),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    assert await asyncio.wait_for(front.get("k", "llm"), 2.0) is None


async def test_cache_stats_counts_names_only() -> None:
    stub = StubRedis()
    front = _front(stub, None)
    secret_key = cache_key("llm", {"input": "the alert text"})
    with track_cache_stats() as stats:
        assert await front.get(secret_key, "llm") is None
        await front.set(secret_key, "llm", {"output": []}, 60)
        assert await front.get(secret_key, "llm") == {"output": []}
        record_cache_lookup("tool", False)

    assert stats.as_metadata() == {
        "cache_llm_hit": 1,
        "cache_llm_miss": 1,
        "cache_tool_hit": 0,
        "cache_tool_miss": 1,
    }
    assert secret_key not in repr(stats.as_metadata())
    # Outside a tracked block nothing is counted (and nothing fails).
    record_cache_lookup("llm", True)
    assert stats.llm_hit == 1
    assert CacheStats().as_metadata() == dict.fromkeys(stats.as_metadata(), 0)


def test_build_cache_matrix() -> None:
    on = Settings(_env_file=None)  # type: ignore[call-arg]
    off = Settings(_env_file=None, sentinel_cache_enabled=False)  # type: ignore[call-arg]
    pool: Any = MagicMock()
    redis = stub_handle(StubRedis())

    assert build_cache(off, pool, redis) is None
    assert build_cache(on, None, None) is None
    assert isinstance(build_cache(on, pool, None), PostgresCache)
    assert isinstance(build_cache(on, pool, redis), RedisFrontCache)
    assert isinstance(build_cache(on, None, redis), RedisFrontCache)
