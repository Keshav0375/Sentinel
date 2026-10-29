"""Local stand-ins for Redis and the Azure credential (no fakeredis, no network).

``StubRedis`` implements :class:`sentinel.infra.redis_client.RedisCommands` over a dict
with per-key expiry on an injectable clock, records every PUBLISH, and raises
``ConnectionError`` on every command while ``fail`` is set. ``StubCredential`` hands out
Entra-shaped access tokens (a JWT with an ``oid`` claim) and counts its calls.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import jwt
from azure.core.credentials import AccessToken

from sentinel.infra.redis_client import EntraRedisToken, RedisHandle
from sentinel.infra.workload_identity import LazyAzureCredential

OID = "00000000-0000-4000-8000-0000000000aa"
# Test-only HMAC key (Sentinel never verifies the token; 32 bytes keeps pyjwt quiet).
_SIGNING_KEY = "sentinel-test-signing-key-32-bytes!!"


class StubRedis:
    """A dict-backed Redis: GET/SET (with ex)/DELETE/PUBLISH, a ``fail`` switch."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.data: dict[str, tuple[str, float | None]] = {}
        self.ttls: dict[str, int | None] = {}
        self.published: list[tuple[str, str]] = []
        self.fail = False
        self.closed = False
        self.calls: list[str] = []

    def _check(self, command: str) -> None:
        self.calls.append(command)
        if self.fail:
            raise ConnectionError("stub redis is down")

    async def get(self, name: str) -> str | None:
        self._check("get")
        entry = self.data.get(name)
        if entry is None:
            return None
        value, expires = entry
        if expires is not None and self._clock() >= expires:
            del self.data[name]
            return None
        return value

    async def set(self, name: str, value: str, ex: int | None = None) -> bool:
        self._check("set")
        self.data[name] = (value, None if ex is None else self._clock() + ex)
        self.ttls[name] = ex
        return True

    async def delete(self, *names: str) -> int:
        self._check("delete")
        return sum(1 for name in names if self.data.pop(name, None) is not None)

    async def publish(self, channel: str, message: str) -> int:
        self._check("publish")
        self.published.append((channel, message))
        return 0

    async def aclose(self) -> None:
        self.closed = True


def entra_token(oid: str | None = OID, **claims: Any) -> str:
    """An Entra-shaped access token (unverified by Sentinel): a JWT with *oid*."""
    body = dict(claims)
    if oid is not None:
        body["oid"] = oid
    return jwt.encode(body, _SIGNING_KEY, algorithm="HS256")


class StubCredential:
    """Stands in for DefaultAzureCredential: ``get_token`` returns the next token."""

    def __init__(self, expires_on: int = 4102444800, *, oid: str | None = OID) -> None:
        self.expires_on = expires_on
        self.oid = oid
        self.scopes: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.scopes)

    def get_token(self, *scopes: str, **_: Any) -> AccessToken:
        self.scopes.extend(scopes)
        return AccessToken(entra_token(self.oid, n=self.calls), self.expires_on)

    def close(self) -> None:
        pass


def lazy(credential: StubCredential) -> LazyAzureCredential:
    """A LazyAzureCredential that builds *credential*."""
    return LazyAzureCredential(lambda: credential)  # type: ignore[arg-type, return-value]


def stub_handle(stub: StubRedis, credential: StubCredential | None = None) -> RedisHandle:
    """A RedisHandle whose every client is *stub*."""
    token = EntraRedisToken(lazy(credential or StubCredential()))
    return RedisHandle("redis.example", 10000, token, factory=lambda *_: stub)
