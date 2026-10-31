"""Tool / LLM result cache — ``cache_entries`` with an optional Redis front (decision
2026-10-25 C).

Entries are keyed by :func:`cache_key`: the SHA-256 of the canonical JSON of the request
(kind, payload, :data:`CACHE_KEY_VERSION`), so a key never carries the request itself.
:class:`PostgresCache` is the durable store; :class:`RedisFrontCache` reads through Redis
first and fills it from Postgres on a hit. Every ``get``/``set`` is bounded by
:data:`CACHE_OP_TIMEOUT_S` and never raises: an unavailable store is a miss, logged as
``cache_unavailable`` (op, kind, error type — never a key or a value). Expired rows are
skipped on read and purged once at startup (:meth:`PostgresCache.purge_expired`).

What is cached is decided by the callers: ``fetch_logs`` results
(:data:`TOOL_CACHE_TTL_S`, ``tools/log_fetcher``) and, when eligible, agent LLM calls
(:data:`LLM_CACHE_TTL_S`, ``providers/cached_model``). The judge never is.

:class:`CacheStats` counts hits and misses per kind for one incident (names only), read by
the orchestrator into its trace metadata and ``pipeline_complete`` log.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Protocol

# asyncpg ships no type stubs (see infra/database.py); only its types are named here.
import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from pydantic import BaseModel, JsonValue

from sentinel.config import Settings
from sentinel.infra import database
from sentinel.infra.logging import get_logger
from sentinel.infra.redis_client import RedisHandle

__all__ = [
    "CACHE_KEY_VERSION",
    "CACHE_OP_TIMEOUT_S",
    "LLM_CACHE_TTL_S",
    "REDIS_CACHE_PREFIX",
    "TOOL_CACHE_TTL_S",
    "Cache",
    "CacheKind",
    "CacheStats",
    "PostgresCache",
    "RedisFrontCache",
    "build_cache",
    "cache_key",
    "record_cache_lookup",
    "track_cache_stats",
]

logger = get_logger("sentinel.memory.cache")

CacheKind = Literal["llm", "tool"]

# Bumped when the key payload changes shape: every older entry then misses.
CACHE_KEY_VERSION: Final = 1
# One cache operation (a store round trip), in seconds; past it the lookup is a miss.
CACHE_OP_TIMEOUT_S: Final = 2.0
# fetch_logs results: 5 minutes, keyed to the minute (decision 2026-10-25 C).
TOOL_CACHE_TTL_S: Final = 300
# An exact-match agent LLM call: 24 hours.
LLM_CACHE_TTL_S: Final = 86400
REDIS_CACHE_PREFIX: Final = "sentinel:cache:"


def _canonical(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return str(value)


def cache_key(kind: CacheKind, payload: Mapping[str, object]) -> str:
    """The cache key of *payload*: SHA-256 hex of its canonical JSON (dict order is irrelevant)."""
    text = json.dumps(
        {"v": CACHE_KEY_VERSION, "kind": kind, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical,
    )
    return hashlib.sha256(text.encode()).hexdigest()


class Cache(Protocol):
    """A key → JSON value cache with a per-entry TTL; neither method ever raises."""

    async def get(self, key: str, kind: CacheKind) -> JsonValue | None: ...

    async def set(self, key: str, kind: CacheKind, value: JsonValue, ttl_s: int) -> None: ...


def _unavailable(op: str, kind: CacheKind, exc: BaseException) -> None:
    logger.warning("cache_unavailable", op=op, kind=kind, error_type=type(exc).__name__)


class PostgresCache:
    """The ``cache_entries`` table."""

    def __init__(self, pool: asyncpg.Pool, *, op_timeout_s: float = CACHE_OP_TIMEOUT_S) -> None:
        self._pool = pool
        self._timeout_s = op_timeout_s

    async def get_entry(self, key: str) -> tuple[JsonValue, datetime] | None:
        """The live entry's value and expiry (its hit count goes up), or None.

        Raises:
            SentinelMemoryError | TimeoutError: The database failed or did not answer.
        """
        async with asyncio.timeout(self._timeout_s):
            async with database.acquire(self._pool) as conn:
                row = await conn.fetchrow(
                    "UPDATE cache_entries SET hit_count = hit_count + 1 "
                    "WHERE key = $1 AND expires_at > now() RETURNING value, expires_at",
                    key,
                )
        if row is None:
            return None
        value: JsonValue = json.loads(row["value"])
        return value, row["expires_at"]

    async def get(self, key: str, kind: CacheKind) -> JsonValue | None:
        try:
            entry = await self.get_entry(key)
        except Exception as exc:
            _unavailable("get", kind, exc)
            entry = None
        record_cache_lookup(kind, entry is not None)
        return None if entry is None else entry[0]

    async def set(self, key: str, kind: CacheKind, value: JsonValue, ttl_s: int) -> None:
        try:
            await self._put(key, kind, value, ttl_s)
        except Exception as exc:
            _unavailable("set", kind, exc)

    async def _put(self, key: str, kind: CacheKind, value: JsonValue, ttl_s: int) -> None:
        async with asyncio.timeout(self._timeout_s):
            async with database.acquire(self._pool) as conn:
                await conn.execute(
                    "INSERT INTO cache_entries (key, kind, value, expires_at) "
                    "VALUES ($1, $2, $3::jsonb, now() + make_interval(secs => $4)) "
                    "ON CONFLICT (key) DO UPDATE SET kind = EXCLUDED.kind, "
                    "value = EXCLUDED.value, expires_at = EXCLUDED.expires_at",
                    key,
                    kind,
                    json.dumps(value),
                    float(ttl_s),
                )

    async def purge_expired(self) -> int:
        """Delete every expired entry; how many. A failure is logged and counts 0."""
        try:
            async with asyncio.timeout(self._timeout_s):
                async with database.acquire(self._pool) as conn:
                    status: str = await conn.execute(
                        "DELETE FROM cache_entries WHERE expires_at <= now()"
                    )
        except Exception as exc:
            logger.warning("cache_purge_failed", error_type=type(exc).__name__)
            return 0
        return int(status.split()[-1])


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RedisFrontCache:
    """Redis in front of an optional :class:`PostgresCache`: read-through, write-both.

    A Redis failure falls through to the backing store (logged once per operation).
    """

    def __init__(
        self,
        redis: RedisHandle,
        backing: PostgresCache | None,
        *,
        op_timeout_s: float = CACHE_OP_TIMEOUT_S,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._redis = redis
        self._backing = backing
        self._timeout_s = op_timeout_s
        self._clock = clock

    async def get(self, key: str, kind: CacheKind) -> JsonValue | None:
        value = await self._lookup(key, kind)
        record_cache_lookup(kind, value is not None)
        return value

    async def _lookup(self, key: str, kind: CacheKind) -> JsonValue | None:
        name = REDIS_CACHE_PREFIX + key
        try:
            async with asyncio.timeout(self._timeout_s):
                raw = await (await self._redis.client()).get(name)
            if raw is not None:
                cached: JsonValue = json.loads(raw)
                return cached
        except Exception as exc:
            _unavailable("redis_get", kind, exc)
        if self._backing is None:
            return None
        try:
            entry = await self._backing.get_entry(key)
        except Exception as exc:
            _unavailable("get", kind, exc)
            return None
        if entry is None:
            return None
        value, expires_at = entry
        remaining = max(1, int((expires_at - self._clock()).total_seconds()))
        try:
            async with asyncio.timeout(self._timeout_s):
                await (await self._redis.client()).set(name, json.dumps(value), ex=remaining)
        except Exception as exc:
            _unavailable("redis_fill", kind, exc)
        return value

    async def set(self, key: str, kind: CacheKind, value: JsonValue, ttl_s: int) -> None:
        if self._backing is not None:
            await self._backing.set(key, kind, value, ttl_s)
        try:
            async with asyncio.timeout(self._timeout_s):
                client = await self._redis.client()
                await client.set(REDIS_CACHE_PREFIX + key, json.dumps(value), ex=ttl_s)
        except Exception as exc:
            _unavailable("redis_set", kind, exc)


# ── Per-incident hit/miss counts (names only) ─────────────────────────────────


@dataclass
class CacheStats:
    """Cache lookups of one incident, by kind."""

    llm_hit: int = 0
    llm_miss: int = 0
    tool_hit: int = 0
    tool_miss: int = 0

    def record(self, kind: CacheKind, hit: bool) -> None:
        if kind == "llm":
            if hit:
                self.llm_hit += 1
            else:
                self.llm_miss += 1
        elif hit:
            self.tool_hit += 1
        else:
            self.tool_miss += 1

    def as_metadata(self) -> dict[str, int]:
        """The counts under their trace-metadata names."""
        return {
            "cache_llm_hit": self.llm_hit,
            "cache_llm_miss": self.llm_miss,
            "cache_tool_hit": self.tool_hit,
            "cache_tool_miss": self.tool_miss,
        }


_CACHE_STATS: ContextVar[CacheStats | None] = ContextVar("sentinel_cache_stats", default=None)


@contextmanager
def track_cache_stats() -> Generator[CacheStats]:
    """Count every cache lookup made inside this block (and the tasks it starts)."""
    stats = CacheStats()
    reset = _CACHE_STATS.set(stats)
    try:
        yield stats
    finally:
        _CACHE_STATS.reset(reset)


def record_cache_lookup(kind: CacheKind, hit: bool) -> None:
    """Count one lookup in the enclosing :func:`track_cache_stats`, if any."""
    stats = _CACHE_STATS.get()
    if stats is not None:
        stats.record(kind, hit)


def build_cache(
    settings: Settings, pool: asyncpg.Pool | None, redis: RedisHandle | None
) -> Cache | None:
    """The process's cache: Redis-fronted when Redis is configured, else Postgres; None when
    ``SENTINEL_CACHE_ENABLED`` is off or neither store exists."""
    if not settings.sentinel_cache_enabled or (pool is None and redis is None):
        return None
    if redis is not None:
        return RedisFrontCache(redis, PostgresCache(pool) if pool is not None else None)
    assert pool is not None
    return PostgresCache(pool)
