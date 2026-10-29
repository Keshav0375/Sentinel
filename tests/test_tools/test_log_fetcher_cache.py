"""Tests for the fetch_logs result cache (decision 2026-10-25 C).

No network: a tiny fake Datadog behind ``httpx.MockTransport`` counts its requests; the
cache is an in-memory stand-in for ``memory.cache.Cache``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import JsonValue

from sentinel.config import Settings
from sentinel.memory.cache import TOOL_CACHE_TTL_S, CacheKind
from sentinel.models.tools import LogFetchInput, LogFetchResult, ToolFailure
from sentinel.tools.log_fetcher import _fetch_logs  # pyright: ignore[reportPrivateUsage]

SERVICE = "sentinel-watchtower"
NOW = datetime(2026, 10, 28, 12, 0, 25, 123456, tzinfo=UTC)


class _DictCache:
    def __init__(self) -> None:
        self.data: dict[str, JsonValue] = {}
        self.sets: list[tuple[str, CacheKind, int]] = []

    async def get(self, key: str, kind: CacheKind) -> JsonValue | None:
        return self.data.get(key)

    async def set(self, key: str, kind: CacheKind, value: JsonValue, ttl_s: int) -> None:
        self.sets.append((key, kind, ttl_s))
        self.data[key] = value


class _Datadog:
    """Answers every read with an empty result; *status* per path prefix overrides it."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for prefix, status in self.status.items():
            if request.url.path.startswith(prefix):
                return httpx.Response(status, json={"errors": ["nope"]})
        if request.url.path == "/api/v1/synthetics/tests":
            return httpx.Response(200, json={"tests": []})
        return httpx.Response(200, json={"data": [], "meta": {"page": {}}})

    def searches(self) -> list[dict[str, Any]]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.url.path == "/api/v2/logs/events/search"
        ]


def _settings() -> Settings:
    return Settings(_env_file=None, dd_api_key="k", dd_app_key="a")  # type: ignore[call-arg]


async def _no_sleep(_: float) -> None:
    return None


async def _call(
    datadog: _Datadog, cache: _DictCache | None, clock: Callable[[], datetime] = lambda: NOW
) -> LogFetchResult | ToolFailure:
    async with httpx.AsyncClient(transport=httpx.MockTransport(datadog)) as client:
        return await _fetch_logs(
            _settings(),
            LogFetchInput(service=SERVICE, window_minutes=60),
            cache=cache,  # type: ignore[arg-type]
            http_client=client,
            sleep=_no_sleep,
            clock=clock,
        )


async def test_second_call_same_minute_is_cached() -> None:
    datadog, cache = _Datadog(), _DictCache()

    first = await _call(datadog, cache)
    reads = len(datadog.requests)
    second = await _call(datadog, cache, clock=lambda: NOW + timedelta(seconds=30))

    assert isinstance(first, LogFetchResult) and isinstance(second, LogFetchResult)
    assert len(datadog.requests) == reads  # served from the cache
    assert second == first
    ceiled = datetime(2026, 10, 28, 12, 1, tzinfo=UTC)  # the next whole minute
    assert first.window.end == ceiled
    assert first.window.start == ceiled - timedelta(minutes=60)
    assert datadog.searches()[0]["filter"]["to"] == "2026-10-28T12:01:00.000Z"
    assert cache.sets == [(cache.sets[0][0], "tool", TOOL_CACHE_TTL_S)]
    assert TOOL_CACHE_TTL_S == 300


async def test_next_minute_is_a_miss() -> None:
    datadog, cache = _Datadog(), _DictCache()

    await _call(datadog, cache)
    reads = len(datadog.requests)
    later = await _call(datadog, cache, clock=lambda: NOW + timedelta(minutes=1))

    assert len(datadog.requests) == 2 * reads
    assert isinstance(later, LogFetchResult)
    assert later.window.end == datetime(2026, 10, 28, 12, 2, tzinfo=UTC)
    assert len(cache.sets) == 2


async def test_partial_failure_not_cached() -> None:
    datadog, cache = _Datadog(), _DictCache()
    datadog.status = {"/api/v2/logs": 500}

    partial = await _call(datadog, cache)
    assert isinstance(partial, LogFetchResult)
    assert partial.sources["synthetics"] == "ok"
    assert partial.sources["deploys"].startswith("failed:")
    reads = len(datadog.requests)

    await _call(datadog, cache)

    assert cache.sets == []
    assert len(datadog.requests) == 2 * reads  # Datadog was queried again


async def test_tool_failure_not_cached() -> None:
    datadog, cache = _Datadog(), _DictCache()
    datadog.status = {"/api": 403}

    failure = await _call(datadog, cache)
    assert isinstance(failure, ToolFailure)
    reads = len(datadog.requests)
    await _call(datadog, cache)

    assert cache.sets == []
    assert len(datadog.requests) == 2 * reads


async def test_an_invalid_entry_is_a_miss() -> None:
    datadog, cache = _Datadog(), _DictCache()
    await _call(datadog, cache)
    (key,) = cache.data
    cache.data[key] = {"not": "a result"}
    reads = len(datadog.requests)

    result = await _call(datadog, cache)

    assert isinstance(result, LogFetchResult)
    assert len(datadog.requests) == 2 * reads


async def test_no_cache_window_end_unrounded() -> None:
    datadog = _Datadog()

    result = await _call(datadog, None)

    assert isinstance(result, LogFetchResult)
    assert result.window.end == NOW
    assert datadog.searches()[0]["filter"]["to"] == "2026-10-28T12:00:25.123Z"


async def test_unconfigured_datadog_never_touches_the_cache() -> None:
    cache = _DictCache()
    result = await _fetch_logs(
        Settings(_env_file=None),  # type: ignore[call-arg]
        LogFetchInput(service=SERVICE),
        cache=cache,  # type: ignore[arg-type]
    )
    assert isinstance(result, ToolFailure) and result.kind == "unavailable"
    assert cache.sets == [] and cache.data == {}
