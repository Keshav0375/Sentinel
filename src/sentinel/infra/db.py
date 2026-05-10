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

# ── Public API ────────────────────────────────────────────────────────────────


async def create_tables(db_path: Path) -> None:
    """Create all tables if they don't exist. Safe to call on every startup.

    Creates parent directories if they don't exist.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as conn:
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
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn
