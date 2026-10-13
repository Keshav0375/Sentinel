"""asyncpg connection pool for PostgreSQL + pgvector (Phase 2).

Runs alongside the Phase-1 SQLite factory in ``infra/db.py`` until that is deleted
(backend.md §13.5). One pool per process: :func:`init_pool` sets the module-level
holder at app startup, :func:`close_pool` drains it at shutdown, and :func:`acquire`
lends a connection from it. Every pooled connection has the pgvector codec
registered, so ``vector`` columns round-trip as :class:`pgvector.Vector`.

Driver failures never leave this module under their own names — they surface as
:class:`~sentinel.exceptions.SentinelMemoryError`.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

# asyncpg ships no type stubs and only partial inline annotations. Pyright runs against
# the runtime package alone, locally and in CI (no asyncpg-stubs), so the few untyped
# calls are pinned to declared types here rather than leaking Unknown to callers.
import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from pgvector.asyncpg import register_vector

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.logging import get_logger

if TYPE_CHECKING:
    from asyncpg.pool import PoolConnectionProxy  # pyright: ignore[reportMissingTypeStubs]

    from sentinel.config import Settings

logger = get_logger("sentinel.infra.database")

# A static password, or a sync/async callable asyncpg invokes on every new connection —
# the hook for short-lived Entra access tokens (backend.md §8.2).
PasswordProvider = str | Callable[[], str] | Callable[[], Awaitable[str]]

# Errors the driver raises for connection, protocol and server failures. OSError covers
# refused/unreachable hosts; TimeoutError covers connect and acquire timeouts.
_DRIVER_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    TimeoutError,
)

# Errors from parsing the DSN or validating connection arguments. Their text can quote
# the DSN — asyncpg reports a password containing "@" as "invalid literal for int():
# '<password tail>@host'" — so only their type name is ever reported or logged, and they
# are never chained (a traceback would print them).
_CONFIG_ERRORS: tuple[type[BaseException], ...] = (
    ValueError,
    asyncpg.ClientConfigurationError,
)

# Errors raised *inside* an acquire() block that belong to the database. Narrower than
# _DRIVER_ERRORS so a caller's own OSError/TimeoutError is not mislabelled.
_QUERY_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
)

_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})

# Upper bound on a graceful close; after this the pool is terminated so shutdown
# never hangs on a connection a stuck request still holds.
_CLOSE_TIMEOUT_S = 10.0

_pool: asyncpg.Pool | None = None


class _ConnectSetupError(Exception):
    """Raised by this module's own connect hooks (password provider, codec setup).

    asyncpg propagates it unchanged out of ``create_pool`` and ``acquire``; its message
    is fixed text, safe to report, and the original error is its ``__cause__``.
    """


def _err(exc: BaseException) -> str:
    """Exception text that is never blank — TimeoutError() has an empty str().

    DSN-parsing and configuration errors report their type and a fixed message only.
    """
    if isinstance(exc, _CONFIG_ERRORS):
        return f"{type(exc).__name__}: invalid DSN or connection parameters"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def is_postgres_dsn(url: str) -> bool:
    """True when *url* is a DSN asyncpg accepts (``postgres://`` or ``postgresql://``)."""
    return urlsplit(url.strip()).scheme.lower() in _POSTGRES_SCHEMES


def _describe(dsn: str) -> dict[str, Any]:
    """Loggable DSN parts — host and database only, never credentials.

    A DSN that does not split cleanly (a bad port, an ``@`` or ``/`` left in the path
    because the password holds a reserved character) is not described at all: its
    pieces could be fragments of the password.
    """
    try:
        parts = urlsplit(dsn)
        port = parts.port
    except ValueError:
        return {"db_dsn": "unparseable"}
    name = parts.path.lstrip("/")
    if "@" in name or "/" in name:
        return {"db_dsn": "unparseable"}
    return {"db_host": parts.hostname, "db_port": port, "db_name": name}


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Pool ``init`` hook: register the pgvector codec on each new connection.

    Requires the ``vector`` extension to exist in the database (alembic migration 001).
    """
    try:
        await register_vector(conn)
    except ValueError as exc:  # "unknown type: public.vector"
        raise _ConnectSetupError(
            "the vector extension is missing (run alembic upgrade head)"
        ) from exc


def _guarded_password(
    password: PasswordProvider | None,
) -> str | Callable[[], Awaitable[str]] | None:
    """Wrap a password callable so whatever it raises surfaces as :class:`_ConnectSetupError`.

    asyncpg invokes the callable on every new connection — eagerly in ``create_pool``
    and lazily in ``acquire`` — and would otherwise let an arbitrary exception (an
    Entra token failure, say) escape this module under its own name.
    """
    if password is None or isinstance(password, str):
        return password
    provider = password

    async def _call() -> str:
        try:
            result = provider()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            raise _ConnectSetupError(f"password provider failed ({type(exc).__name__})") from exc
        return result

    return _call


async def create_pool(
    dsn: str,
    *,
    min_size: int = 2,
    max_size: int = 10,
    password: PasswordProvider | None = None,
) -> asyncpg.Pool:
    """Create an asyncpg pool with the pgvector codec on every connection.

    Opens ``min_size`` connections eagerly, so an unreachable server or a database
    without the ``vector`` extension fails here rather than on the first query.

    Args:
        dsn: ``postgresql://`` connection string (``DATABASE_URL``).
        min_size: Connections kept open (``SENTINEL_DB_POOL_MIN``).
        max_size: Hard cap on concurrent connections (``SENTINEL_DB_POOL_MAX``).
        password: Overrides any password in *dsn*. A callable (sync or async) is
            invoked for every new connection. The deployed server is password-less,
            Entra-only: there this is a callable returning a fresh Entra access token.
            Local and CI databases put the password in the DSN and leave this None.

    Raises:
        SentinelMemoryError: the pool could not be created (bad DSN or bounds,
            unreachable server, auth failure, a failing *password* callable,
            missing ``vector`` extension).
    """
    if min_size < 0 or max_size < 1 or min_size > max_size:
        raise SentinelMemoryError(
            f"invalid database pool bounds: min_size={min_size}, max_size={max_size}"
        )
    try:
        pool: asyncpg.Pool = await asyncpg.create_pool(  # pyright: ignore[reportUnknownMemberType]
            dsn,
            min_size=min_size,
            max_size=max_size,
            password=_guarded_password(password),
            init=_init_connection,
        )
    except _ConnectSetupError as exc:
        logger.error("db_pool_create_failed", error=str(exc), **_describe(dsn))
        raise SentinelMemoryError(f"could not create database pool: {exc}") from exc.__cause__
    except _CONFIG_ERRORS as exc:
        logger.error("db_pool_create_failed", error=_err(exc), **_describe(dsn))
        raise SentinelMemoryError(f"could not create database pool: {_err(exc)}") from None
    except _DRIVER_ERRORS as exc:
        logger.error("db_pool_create_failed", error=_err(exc), **_describe(dsn))
        raise SentinelMemoryError(f"could not create database pool: {_err(exc)}") from exc
    logger.info("db_pool_created", min_size=min_size, max_size=max_size, **_describe(dsn))
    return pool


async def init_pool(
    dsn: str,
    *,
    min_size: int = 2,
    max_size: int = 10,
    password: PasswordProvider | None = None,
) -> asyncpg.Pool:
    """Create the process-wide pool and store it in the module-level holder.

    Call once from the app lifespan at startup; pair with :func:`close_pool`.

    Raises:
        SentinelMemoryError: a pool is already initialised, or creation failed.
    """
    global _pool  # noqa: PLW0603
    if _pool is not None:
        raise SentinelMemoryError("database pool is already initialised")
    _pool = await create_pool(dsn, min_size=min_size, max_size=max_size, password=password)
    return _pool


async def close_pool() -> None:
    """Close the process-wide pool, if any. Safe to call when none was created.

    Waits up to ``_CLOSE_TIMEOUT_S`` for borrowed connections to come back, then
    terminates the rest.
    """
    global _pool  # noqa: PLW0603
    pool, _pool = _pool, None
    if pool is None:
        return
    try:
        await asyncio.wait_for(pool.close(), timeout=_CLOSE_TIMEOUT_S)
    except TimeoutError:
        logger.warning("db_pool_close_timeout", timeout_s=_CLOSE_TIMEOUT_S)
        pool.terminate()
    logger.info("db_pool_closed")


async def init_pool_from_settings(settings: Settings) -> asyncpg.Pool | None:
    """Lifespan entry point: create the pool only when ``DATABASE_URL`` is Postgres.

    No ``DATABASE_URL`` (or a non-Postgres one) is not an error while the Phase-1
    SQLite store is still live — the app boots without a pool and returns None.

    Raises:
        SentinelMemoryError: a Postgres DSN is configured but the pool failed.
    """
    url = settings.database_url.strip()
    if not url:
        logger.info("db_pool_skipped", reason="DATABASE_URL not set")
        return None
    if not is_postgres_dsn(url):
        logger.warning(
            "db_pool_skipped",
            reason="DATABASE_URL is not a postgres DSN",
            scheme=urlsplit(url).scheme,
        )
        return None
    return await init_pool(
        url, min_size=settings.sentinel_db_pool_min, max_size=settings.sentinel_db_pool_max
    )


def get_pool() -> asyncpg.Pool:
    """Return the process-wide pool.

    Raises:
        SentinelMemoryError: :func:`init_pool` has not run (no ``DATABASE_URL``).
    """
    if _pool is None:
        raise SentinelMemoryError("database pool is not initialised")
    return _pool


@asynccontextmanager
async def acquire(
    pool: asyncpg.Pool | None = None, *, timeout: float | None = None
) -> AsyncGenerator[PoolConnectionProxy]:
    """Borrow a connection; it goes back to the pool when the block exits.

    Uses the process-wide pool unless *pool* is given (tests, explicit injection).

    Usage::

        async with acquire() as conn:
            row = await conn.fetchrow("SELECT ...")

    Raises:
        SentinelMemoryError: no pool, acquire failed or timed out (a new connection's
            *password* callable failing included), or a database error was raised
            inside the block.
    """
    target = pool if pool is not None else get_pool()
    try:
        conn = cast(
            "PoolConnectionProxy",
            await target.acquire(timeout=timeout),  # pyright: ignore[reportUnknownMemberType]
        )
    except _ConnectSetupError as exc:
        logger.error("db_acquire_failed", error=str(exc))
        raise SentinelMemoryError(f"could not acquire a database connection: {exc}") from (
            exc.__cause__
        )
    except _CONFIG_ERRORS as exc:
        logger.error("db_acquire_failed", error=_err(exc))
        raise SentinelMemoryError(f"could not acquire a database connection: {_err(exc)}") from None
    except _DRIVER_ERRORS as exc:
        logger.error("db_acquire_failed", error=_err(exc))
        raise SentinelMemoryError(f"could not acquire a database connection: {_err(exc)}") from exc
    try:
        yield conn
    except _QUERY_ERRORS as exc:
        raise SentinelMemoryError(f"database operation failed: {_err(exc)}") from exc
    finally:
        try:
            await target.release(conn)  # pyright: ignore[reportUnknownMemberType]
        except _DRIVER_ERRORS as exc:
            # asyncpg terminates a connection it cannot reset, so nothing leaks;
            # surfacing this would mask the block's own outcome.
            logger.warning("db_release_failed", error=_err(exc))
