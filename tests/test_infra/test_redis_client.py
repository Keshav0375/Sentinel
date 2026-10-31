"""Tests for sentinel.infra.redis_client — Entra token, client rebuilds, the fallback.

No network: the credential is a stub and every client is the local Redis stub, except the
TLS test, which builds a real ``redis.asyncio.Redis`` without connecting it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import redis.asyncio
from redis_stub import OID, StubCredential, StubRedis, entra_token, lazy

from sentinel.config import Settings
from sentinel.exceptions import SentinelError
from sentinel.infra import redis_client as redis_module
from sentinel.infra.redis_client import (
    REDIS_ENTRA_SCOPE,
    REDIS_TOKEN_REFRESH_MARGIN_S,
    EntraRedisToken,
    RedisHandle,
    build_redis,
)
from sentinel.infra.workload_identity import LazyAzureCredential

EXPIRES = 2_000_000_000


def test_build_redis_none_without_host(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(redis_module, "logger", log)
    factory = MagicMock()
    credential = LazyAzureCredential(factory)

    assert build_redis(Settings(_env_file=None), credential) is None  # type: ignore[call-arg]

    assert not credential.built
    factory.assert_not_called()
    log.info.assert_called_once_with("redis_skipped", reason="REDIS_HOST not set")


async def test_token_username_is_oid_and_scope() -> None:
    credential = StubCredential(expires_on=EXPIRES)
    built: list[tuple[str, int, str, str]] = []
    stub = StubRedis()

    def factory(host: str, port: int, username: str, password: str) -> StubRedis:
        built.append((host, port, username, password))
        return stub

    settings = Settings(_env_file=None, redis_host="sentinel.redis.azure.net")  # type: ignore[call-arg]
    handle = build_redis(settings, lazy(credential), factory=factory)
    assert handle is not None
    assert credential.calls == 0  # nothing connects at build time

    first, second = await handle.client(), await handle.client()
    await first.set("k", "v")
    assert await second.get("k") == "v" and stub.data["k"][0] == "v"  # both reach the stub

    assert credential.scopes == [REDIS_ENTRA_SCOPE] == ["https://redis.azure.com/.default"]
    ((host, port, username, password),) = built
    assert (host, port, username) == ("sentinel.redis.azure.net", 10000, OID)
    assert password.count(".") == 2  # the access token (a JWT) is the password


async def test_token_refreshed_before_expiry() -> None:
    credential = StubCredential(expires_on=EXPIRES)
    now = [EXPIRES - REDIS_TOKEN_REFRESH_MARGIN_S - 1]
    token = EntraRedisToken(lazy(credential), clock=lambda: now[0])
    clients: list[StubRedis] = []

    def factory(host: str, port: int, username: str, password: str) -> StubRedis:
        clients.append(StubRedis())
        return clients[-1]

    handle = RedisHandle("h", 10000, token, factory=factory)
    first = await handle.client()
    assert credential.calls == 1

    now[0] = EXPIRES - 299  # inside the 300 s margin: fetch again
    credential.expires_on = EXPIRES + 3600
    second = await handle.client()

    assert credential.calls == 2
    assert len(clients) == 2
    await second.set("k", "v")
    assert clients[1].calls == ["set"] and clients[0].calls == []  # the new client serves
    await asyncio.sleep(0)
    assert clients[0].closed  # idle when retired: closed in the background at once
    await handle.aclose()
    assert clients[1].closed
    assert first is not second


async def test_token_without_oid_raises() -> None:
    token = EntraRedisToken(lazy(StubCredential(oid=None)))
    with pytest.raises(SentinelError, match="no oid claim"):
        await token.current()


def test_entra_token_helper_is_a_jwt() -> None:
    assert entra_token().count(".") == 2


async def test_ssl_always_on() -> None:
    client = redis_module._redis_client("sentinel.redis.azure.net", 10000, OID, "tok")  # pyright: ignore[reportPrivateUsage]
    assert isinstance(client, redis.asyncio.Redis)
    pool = client.connection_pool
    assert pool.connection_class is redis.asyncio.SSLConnection
    kwargs = pool.connection_kwargs
    assert kwargs["host"] == "sentinel.redis.azure.net" and kwargs["port"] == 10000
    assert kwargs["username"] == OID and kwargs["password"] == "tok"
    await client.aclose()


async def test_aclose_never_raises() -> None:
    class _Broken(StubRedis):
        async def aclose(self) -> None:
            raise ConnectionError("gone")

    handle = RedisHandle(
        "h", 10000, EntraRedisToken(lazy(StubCredential())), factory=lambda *_: _Broken()
    )
    await handle.client()
    await handle.aclose()
    await handle.aclose()  # twice is fine


async def test_refresh_closes_the_old_client_only_after_in_flight_commands() -> None:
    credential = StubCredential(expires_on=EXPIRES)
    now = [EXPIRES - REDIS_TOKEN_REFRESH_MARGIN_S - 1]
    token = EntraRedisToken(lazy(credential), clock=lambda: now[0])
    release = asyncio.Event()

    class _Slow(StubRedis):
        async def get(self, name: str) -> str | None:
            await release.wait()
            assert not self.closed, "closed under an in-flight command"
            return await super().get(name)

    clients: list[StubRedis] = []

    def factory(host: str, port: int, username: str, password: str) -> StubRedis:
        clients.append(_Slow() if not clients else StubRedis())
        return clients[-1]

    handle = RedisHandle("h", 10000, token, factory=factory)
    old = await handle.client()
    await clients[0].set("k", "old")
    in_flight = asyncio.create_task(old.get("k"))
    await asyncio.sleep(0)

    now[0] = EXPIRES - 299  # the token is due: the next client() swaps in a new client
    credential.expires_on = EXPIRES + 3600
    new = await handle.client()
    await new.set("k", "new")
    for _ in range(3):
        await asyncio.sleep(0)
    assert not clients[0].closed  # its command is still running

    release.set()
    assert await in_flight == "old"  # finished on the old client, not failed mid-flight
    for _ in range(3):
        await asyncio.sleep(0)
    assert clients[0].closed  # closed once idle
    assert not clients[1].closed
    await handle.aclose()
    assert clients[1].closed


async def test_aclose_waits_for_a_retired_clients_commands() -> None:
    release = asyncio.Event()

    class _Gate(StubRedis):
        async def get(self, name: str) -> str | None:
            await release.wait()
            assert not self.closed, "closed under an in-flight command"
            return await super().get(name)

    gate = _Gate()
    handle = RedisHandle(
        "h",
        10000,
        EntraRedisToken(lazy(StubCredential(expires_on=EXPIRES))),
        factory=lambda *_: gate,
    )
    leased = await handle.client()
    running = asyncio.create_task(leased.get("k"))
    await asyncio.sleep(0)
    closing = asyncio.create_task(handle.aclose())
    await asyncio.sleep(0)
    assert not closing.done() and not gate.closed  # waiting for the command
    release.set()
    assert await running is None
    await closing
    assert gate.closed
