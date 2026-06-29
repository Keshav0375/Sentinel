"""Async SQLite connection factory — aiosqlite context manager."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

# ── DDL ───────────────────────────────────────────────────────────────────────

_CREATE_EPISODIC_INCIDENTS = """
CREATE TABLE IF NOT EXISTS episodic_incidents (
    id           TEXT      PRIMARY KEY,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    service_name TEXT      NOT NULL,
    severity     TEXT      NOT NULL,
    symptoms     TEXT      NOT NULL,
    root_cause   TEXT      NOT NULL,
    resolution   TEXT      NOT NULL,
    mttr_seconds INTEGER,
    embedding    BLOB,
    raw_timeline JSON
);
"""

_CREATE_SERVICES = """
CREATE TABLE IF NOT EXISTS services (
    name           TEXT PRIMARY KEY,
    team           TEXT NOT NULL,
    tier           TEXT NOT NULL,
    oncall_channel TEXT,
    repo_url       TEXT,
    description    TEXT,
    dependencies   JSON
);
"""

_CREATE_RUNBOOKS = """
CREATE TABLE IF NOT EXISTS runbooks (
    id            TEXT PRIMARY KEY,
    service_name  TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    steps         JSON NOT NULL,
    FOREIGN KEY (service_name) REFERENCES services(name)
);
"""

_ALL_DDL: list[str] = [
    _CREATE_EPISODIC_INCIDENTS,
    _CREATE_SERVICES,
    _CREATE_RUNBOOKS,
]

_SHARED_MEMORY_URI = "file::memory:?cache=shared"

# Keeps the shared in-memory DB alive across connections. Without this,
# closing the last connection destroys all tables.
_keepalive: aiosqlite.Connection | None = None


def _is_memory(db_path: Path) -> bool:
    return str(db_path) == ":memory:"


def _connect(db_path: Path) -> aiosqlite.Connection:
    if _is_memory(db_path):
        return aiosqlite.connect(_SHARED_MEMORY_URI, uri=True)
    return aiosqlite.connect(db_path)


async def _ensure_keepalive() -> None:
    global _keepalive  # noqa: PLW0603
    if _keepalive is None:
        conn = aiosqlite.connect(_SHARED_MEMORY_URI, uri=True)
        _keepalive = await conn.__aenter__()


async def close_keepalive() -> None:
    """Close the keep-alive connection (call on shutdown)."""
    global _keepalive  # noqa: PLW0603
    if _keepalive is not None:
        await _keepalive.close()
        _keepalive = None


# ── Public API ────────────────────────────────────────────────────────────────


async def create_tables(db_path: Path) -> None:
    """Create all tables if they don't exist. Safe to call on every startup.

    Creates parent directories if they don't exist.
    """
    if _is_memory(db_path):
        await _ensure_keepalive()
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    async with _connect(db_path) as conn:
        for ddl in _ALL_DDL:
            await conn.execute(ddl)
        await conn.commit()


@asynccontextmanager
async def get_db(db_path: Path) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager yielding an open, configured aiosqlite connection.

    Enables WAL mode for better concurrent read/write throughput and enforces
    foreign key constraints on every connection.

    Usage::

        async with get_db(settings.sentinel_db_path) as conn:
            await conn.execute("SELECT ...")
    """
    async with _connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        if not _is_memory(db_path):
            await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn
