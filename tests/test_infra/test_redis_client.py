"""Tests for sentinel.infra.redis_client — Entra token, client rebuilds, the fallback.

No network: the credential is a stub and every client is the local Redis stub, except the
TLS test, which builds a real ``redis.asyncio.Redis`` without connecting it.
"""

from __future__ import annotations

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

    assert await handle.client() is stub
    assert await handle.client() is stub  # the token is reused: one fetch, one client

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
    assert second is not first and len(clients) == 2
    await handle.aclose()
    assert clients[0].closed and clients[1].closed  # the old one closed in the background


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
