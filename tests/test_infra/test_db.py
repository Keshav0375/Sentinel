"""Tests for sentinel.infra.db."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.infra.db import create_tables, get_db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.mark.asyncio
async def test_create_tables_is_idempotent(db_path: Path) -> None:
    await create_tables(db_path)
    await create_tables(db_path)  # second call must not raise
    assert db_path.exists()


@pytest.mark.asyncio
async def test_tables_exist_after_create(db_path: Path) -> None:
    await create_tables(db_path)
    async with get_db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in await cursor.fetchall()}
    assert tables == {"episodic_incidents", "services", "runbooks"}


@pytest.mark.asyncio
async def test_get_db_wal_mode(db_path: Path) -> None:
    await create_tables(db_path)
    async with get_db(db_path) as conn:
        cursor = await conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "wal"


@pytest.mark.asyncio
async def test_get_db_foreign_keys_enabled(db_path: Path) -> None:
    await create_tables(db_path)
    async with get_db(db_path) as conn:
        cursor = await conn.execute("PRAGMA foreign_keys")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1


@pytest.mark.asyncio
async def test_insert_and_query_service(db_path: Path) -> None:
    await create_tables(db_path)
    async with get_db(db_path) as conn:
        await conn.execute(
            "INSERT INTO services (name, team, tier) VALUES (?, ?, ?)",
            ("api-gateway", "team-platform", "critical"),
        )
        await conn.commit()
        cursor = await conn.execute("SELECT name, team FROM services")
        row = await cursor.fetchone()
        assert row is not None
        assert row["name"] == "api-gateway"
        assert row["team"] == "team-platform"


@pytest.mark.asyncio
async def test_create_tables_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "sentinel.db"
    await create_tables(nested)
    assert nested.exists()
