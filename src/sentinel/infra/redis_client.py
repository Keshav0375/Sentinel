"""Azure Managed Redis — an Entra-authenticated, TLS-only client (decision 2026-10-25 D,
amended 2026-10-28).

Redis is optional and non-durable: it fronts the cache, mirrors short-term memory and
carries the status events. ``REDIS_HOST`` unset → :func:`build_redis` returns None and
every feature uses its fallback. Nothing connects at import or at boot; the first use does.

Auth is Entra, never an access key: the password is an access token for
:data:`REDIS_ENTRA_SCOPE` from the shared workload-identity credential, the username the
token's ``oid`` claim (read without verification — the token came from Entra, and Redis
verifies it). The token is reused until :data:`REDIS_TOKEN_REFRESH_MARGIN_S` before it
expires; on a refresh :class:`RedisHandle` builds a NEW client (its connections AUTH with the
new token) and closes the old one in the background, so no connection outlives its token.
Tokens are never logged.

Production code depends only on the narrow :class:`RedisCommands` protocol (the tests use
a local stub).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Final, Protocol, cast

import jwt
import redis.asyncio

from sentinel.config import Settings
from sentinel.exceptions import SentinelError
from sentinel.infra.logging import get_logger
from sentinel.infra.workload_identity import LazyAzureCredential

__all__ = [
    "REDIS_ENTRA_SCOPE",
    "REDIS_TOKEN_REFRESH_MARGIN_S",
    "ClientFactory",
    "EntraRedisToken",
    "RedisCommands",
    "RedisHandle",
    "build_redis",
]

logger = get_logger("sentinel.infra.redis_client")

REDIS_ENTRA_SCOPE: Final = "https://redis.azure.com/.default"
# A token is replaced this many seconds before it expires.
REDIS_TOKEN_REFRESH_MARGIN_S: Final = 300.0
# Every Redis socket operation and connect, in seconds.
_SOCKET_TIMEOUT_S: Final = 5.0


class RedisCommands(Protocol):
    """The Redis commands Sentinel uses — all a client (or a test stub) must provide."""

    async def get(self, name: str) -> bytes | str | None: ...

    async def set(self, name: str, value: str, ex: int | None = None) -> object: ...

    async def delete(self, *names: str) -> object: ...

    async def publish(self, channel: str, message: str) -> object: ...

    async def aclose(self) -> None: ...


class EntraRedisToken:
    """The current Entra token for Redis and its ``oid``, refreshed before it expires."""

    def __init__(
        self, credential: LazyAzureCredential, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._credential = credential
        self._clock = clock
        self._current: tuple[str, str, float] | None = None

    async def current(self) -> tuple[str, str, float]:
        """``(oid, token, expires_on)``; fetched again when none is held or it is due.

        Raises:
            SentinelError: The token carries no ``oid`` claim.
        """
        held = self._current
        if held is not None and self._clock() < held[2] - REDIS_TOKEN_REFRESH_MARGIN_S:
            return held
        access = await asyncio.to_thread(self._credential.get().get_token, REDIS_ENTRA_SCOPE)
        claims = cast(
            dict[str, object], jwt.decode(access.token, options={"verify_signature": False})
        )
        oid = claims.get("oid")
        if not isinstance(oid, str) or not oid:
            raise SentinelError("the Redis Entra token has no oid claim")
        self._current = (oid, access.token, float(access.expires_on))
        logger.info("redis_token_refreshed", expires_on=int(access.expires_on))
        return self._current


# (host, port, username, password) → a client.
ClientFactory = Callable[[str, int, str, str], RedisCommands]


def _redis_client(host: str, port: int, username: str, password: str) -> RedisCommands:
    """A ``redis.asyncio.Redis`` over TLS, authenticated as *username* with the token."""
    client = redis.asyncio.Redis(
        host=host,
        port=port,
        ssl=True,
        username=username,
        password=password,
        decode_responses=True,
        socket_timeout=_SOCKET_TIMEOUT_S,
        socket_connect_timeout=_SOCKET_TIMEOUT_S,
    )
    return cast(RedisCommands, client)


class RedisHandle:
    """The live Redis client, rebuilt whenever the Entra token is replaced."""

    def __init__(
        self,
        host: str,
        port: int,
        token: EntraRedisToken,
        *,
        factory: ClientFactory = _redis_client,
    ) -> None:
        self.host = host
        self.port = port
        self._token = token
        self._factory = factory
        self._lock = asyncio.Lock()
        self._client: RedisCommands | None = None
        self._built_for: float | None = None
        self._closing: set[asyncio.Task[None]] = set()

    async def client(self) -> RedisCommands:
        """The client for the current token (a new one after a refresh).

        Raises:
            Exception: The token could not be fetched (callers log and fall back).
        """
        async with self._lock:
            oid, token, expires_on = await self._token.current()
            if self._client is None or self._built_for != expires_on:
                old = self._client
                self._client = self._factory(self.host, self.port, oid, token)
                self._built_for = expires_on
                if old is not None:
                    task = asyncio.create_task(self._close(old), name="redis-client-close")
                    self._closing.add(task)
                    task.add_done_callback(self._closing.discard)
            return self._client

    async def aclose(self) -> None:
        """Close the live client and wait for the pending closes. Never raises."""
        client, self._client = self._client, None
        if client is not None:
            await self._close(client)
        if self._closing:
            await asyncio.wait(set(self._closing))

    @staticmethod
    async def _close(client: RedisCommands) -> None:
        try:
            await client.aclose()
        except Exception as exc:
            logger.warning("redis_client_close_failed", error_type=type(exc).__name__)


def build_redis(
    settings: Settings,
    credential: LazyAzureCredential,
    *,
    factory: ClientFactory | None = None,
) -> RedisHandle | None:
    """The Redis handle for ``REDIS_HOST``, or None when it is unset. Connects nothing."""
    host = settings.redis_host
    if host is None:
        logger.info("redis_skipped", reason="REDIS_HOST not set")
        return None
    logger.info("redis_configured", host=host, port=settings.redis_port)
    return RedisHandle(
        host,
        settings.redis_port,
        EntraRedisToken(credential),
        factory=factory if factory is not None else _redis_client,
    )
