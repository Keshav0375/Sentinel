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
new token) and swaps it in. The old client is closed only once the commands in flight on it
have finished: :meth:`RedisHandle.client` hands out a lease that counts each command on the
client it was built for, and a retired client closes when its count drops to zero — a
command never fails mid-flight because of a refresh, and no connection outlives the margin
(the old token is still valid for those last commands). Tokens are never logged.

Production code depends only on the narrow :class:`RedisCommands` protocol (the tests use
a local stub).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Final, Protocol, TypeVar, cast

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
# At shutdown: how long a retired client's commands may still run before it is closed.
_DRAIN_WAIT_S: Final = 2 * _SOCKET_TIMEOUT_S

_T = TypeVar("_T")


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


class _Lease:
    """One built client and the commands running on it; closed once retired and idle."""

    def __init__(self, client: RedisCommands) -> None:
        self.client = client
        self.in_flight = 0
        self.retired = False
        self.closed = False
        self.idle = asyncio.Event()
        self.idle.set()


class _LeasedClient:
    """What :meth:`RedisHandle.client` returns: each command counted on its lease while it
    runs. The handle owns the client, so :meth:`aclose` here is a no-op."""

    def __init__(self, handle: RedisHandle, lease: _Lease) -> None:
        self._handle = handle
        self._lease = lease

    async def get(self, name: str) -> bytes | str | None:
        return await self._handle._run(self._lease, lambda c: c.get(name))  # pyright: ignore[reportPrivateUsage]

    async def set(self, name: str, value: str, ex: int | None = None) -> object:
        return await self._handle._run(self._lease, lambda c: c.set(name, value, ex=ex))  # pyright: ignore[reportPrivateUsage]

    async def delete(self, *names: str) -> object:
        return await self._handle._run(self._lease, lambda c: c.delete(*names))  # pyright: ignore[reportPrivateUsage]

    async def publish(self, channel: str, message: str) -> object:
        return await self._handle._run(self._lease, lambda c: c.publish(channel, message))  # pyright: ignore[reportPrivateUsage]

    async def aclose(self) -> None:
        return None


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
        self._lease: _Lease | None = None
        self._built_for: float | None = None
        # Retired leases with commands still in flight, closed when they go idle.
        self._retired: set[_Lease] = set()
        self._closing: set[asyncio.Task[None]] = set()

    async def client(self) -> RedisCommands:
        """The client for the current token (a new one after a refresh), as a lease.

        Raises:
            Exception: The token could not be fetched (callers log and fall back).
        """
        async with self._lock:
            oid, token, expires_on = await self._token.current()
            if self._lease is None or self._built_for != expires_on:
                old = self._lease
                self._lease = _Lease(self._factory(self.host, self.port, oid, token))
                self._built_for = expires_on
                if old is not None:
                    self._retire(old)
            return _LeasedClient(self, self._lease)

    async def aclose(self) -> None:
        """Close every client: the live one at once, a retired one once its commands end
        (waiting at most :data:`_DRAIN_WAIT_S`). Never raises."""
        lease, self._lease = self._lease, None
        if lease is not None:
            self._retire(lease)
        for retired in list(self._retired):
            try:
                await asyncio.wait_for(retired.idle.wait(), _DRAIN_WAIT_S)
            except TimeoutError:
                logger.warning("redis_client_closed_busy", in_flight=retired.in_flight)
            self._close_later(retired)
        if self._closing:
            await asyncio.wait(set(self._closing))

    async def _run(self, lease: _Lease, call: Callable[[RedisCommands], Awaitable[_T]]) -> _T:
        """Run one command on *lease*'s client, counted while it runs."""
        lease.in_flight += 1
        lease.idle.clear()
        try:
            return await call(lease.client)
        finally:
            lease.in_flight -= 1
            if lease.in_flight == 0:
                lease.idle.set()
                if lease.retired:
                    self._close_later(lease)

    def _retire(self, lease: _Lease) -> None:
        """Swap *lease* out: closed now when idle, else when its last command ends."""
        lease.retired = True
        if lease.in_flight == 0:
            self._close_later(lease)
        else:
            self._retired.add(lease)

    def _close_later(self, lease: _Lease) -> None:
        if lease.closed:
            return
        lease.closed = True
        self._retired.discard(lease)
        task = asyncio.create_task(self._close(lease.client), name="redis-client-close")
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

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
