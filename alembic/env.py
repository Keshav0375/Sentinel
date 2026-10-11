"""Alembic environment — async (asyncpg) migrations for the Postgres + pgvector schema.

The DSN is ``DATABASE_URL`` — the same ``postgresql://`` string the app's asyncpg pool
uses (backend.md §5). Resolution order: ``config.attributes["database_url"]`` (set by
callers that drive alembic programmatically, e.g. tests), the ``DATABASE_URL``
environment variable, then ``DATABASE_URL`` in ``./.env``.

The engine runs on SQLAlchemy's ``postgresql+asyncpg`` dialect, but connections are
opened by ``asyncpg.connect(dsn)`` itself (``async_creator``), so the DSN is parsed
exactly as the app pool parses it — ``postgres://``/``postgresql://`` schemes and libpq
query options such as ``sslmode`` included. A DSN without a password falls back to
``PGPASSWORD``, which is how an Entra access token is supplied to the password-less
Azure server.

Migrations are hand-written SQL (``op.execute``); there is no ORM metadata, so
``--autogenerate`` is not supported.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

import asyncpg
from alembic import context
from alembic.util import CommandError
from dotenv import dotenv_values
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from sentinel.infra.database import is_postgres_dsn

config = context.config

# Programmatic callers pass configure_logger=False to keep their own logging setup.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None


def _database_url() -> str:
    """The Postgres DSN to migrate, from attributes → environment → ``.env``."""
    url = config.attributes.get("database_url")
    if url is None:
        url = os.environ.get("DATABASE_URL")
    if url is None:
        url = dotenv_values(".env").get("DATABASE_URL")
    url = (url or "").strip()
    if not url:
        raise CommandError("DATABASE_URL is not set — point it at a pgvector Postgres DSN")
    if not is_postgres_dsn(url):
        raise CommandError("DATABASE_URL is not a postgres:// or postgresql:// DSN")
    return url


def run_migrations_offline() -> None:
    """Emit the migration SQL to stdout (``alembic upgrade head --sql``) — no connection."""
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run every pending migration in one transaction on an asyncpg connection."""
    dsn = _database_url()

    async def _connect() -> asyncpg.Connection:
        return await asyncpg.connect(dsn)

    engine = create_async_engine(
        "postgresql+asyncpg://", async_creator=_connect, poolclass=pool.NullPool
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
