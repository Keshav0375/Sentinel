"""Tests for data.seed — SQLite seeding of services and runbooks."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from data.seed import seed

# ── Helpers ───────────────────────────────────────────────────────────────────


async def _fetch_all(db_path: Path, sql: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(sql) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]


# ── Basic seeding ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_returns_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    services_count, runbooks_count = await seed(db_path, verbose=False)
    assert services_count == 8
    assert runbooks_count == 19


@pytest.mark.asyncio
async def test_seed_creates_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "subdir" / "sentinel.db"
    await seed(db_path, verbose=False)
    assert db_path.exists()


@pytest.mark.asyncio
async def test_seed_inserts_all_services(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    rows = await _fetch_all(db_path, "SELECT name FROM services ORDER BY name")
    names = [r["name"] for r in rows]
    assert len(names) == 8
    assert "api-gateway" in names
    assert "auth-service" in names
    assert "payment-service" in names
    assert "analytics-pipeline" in names


@pytest.mark.asyncio
async def test_seed_inserts_all_runbooks(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    rows = await _fetch_all(db_path, "SELECT id FROM runbooks")
    assert len(rows) == 19


# ── Service field integrity ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_service_fields_populated(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    rows = await _fetch_all(db_path, "SELECT * FROM services")
    for row in rows:
        assert row["name"]
        assert row["team"]
        assert row["tier"] in ("critical", "standard", "best-effort")
        assert row["oncall_channel"] and row["oncall_channel"].startswith("#")
        assert row["repo_url"] and row["repo_url"].startswith("https://github.com/")


@pytest.mark.asyncio
async def test_seed_service_dependencies_is_valid_json(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    rows = await _fetch_all(db_path, "SELECT name, dependencies FROM services")
    for row in rows:
        deps = json.loads(row["dependencies"])
        assert isinstance(deps, list), f"{row['name']}: dependencies should be a JSON array"


@pytest.mark.asyncio
async def test_seed_api_gateway_has_dependencies(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    rows = await _fetch_all(db_path, "SELECT dependencies FROM services WHERE name='api-gateway'")
    deps = json.loads(rows[0]["dependencies"])
    assert "auth-service" in deps
    assert "payment-service" in deps


@pytest.mark.asyncio
async def test_seed_leaf_service_has_empty_deps(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    rows = await _fetch_all(db_path, "SELECT dependencies FROM services WHERE name='user-service'")
    deps = json.loads(rows[0]["dependencies"])
    assert deps == []


# ── Runbook field integrity ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_runbook_steps_is_valid_json(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    rows = await _fetch_all(db_path, "SELECT id, steps FROM runbooks")
    for row in rows:
        steps = json.loads(row["steps"])
        assert isinstance(steps, list), f"{row['id']}: steps should be a JSON array"
        assert len(steps) >= 3, f"{row['id']}: runbook needs at least 3 steps"


@pytest.mark.asyncio
async def test_seed_runbooks_have_valid_service_names(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    service_rows = await _fetch_all(db_path, "SELECT name FROM services")
    service_names = {r["name"] for r in service_rows}
    runbook_rows = await _fetch_all(db_path, "SELECT service_name FROM runbooks")
    for row in runbook_rows:
        assert row["service_name"] in service_names, (
            f"Runbook references unknown service: {row['service_name']}"
        )


@pytest.mark.asyncio
async def test_seed_each_service_has_at_least_one_runbook(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    rows = await _fetch_all(
        db_path,
        "SELECT service_name, COUNT(*) as cnt FROM runbooks GROUP BY service_name",
    )
    services_with_runbooks = {r["service_name"] for r in rows}
    all_services = {r["name"] for r in await _fetch_all(db_path, "SELECT name FROM services")}
    assert services_with_runbooks == all_services, (
        f"Services without runbooks: {all_services - services_with_runbooks}"
    )


# ── Idempotency ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    # First run
    svc1, rb1 = await seed(db_path, verbose=False)
    # Second run — should not raise or create duplicates
    svc2, rb2 = await seed(db_path, verbose=False)
    assert svc1 == svc2
    assert rb1 == rb2
    # Verify no duplicate rows
    rows = await _fetch_all(db_path, "SELECT COUNT(*) as cnt FROM services")
    assert rows[0]["cnt"] == 8
    rb_rows = await _fetch_all(db_path, "SELECT COUNT(*) as cnt FROM runbooks")
    assert rb_rows[0]["cnt"] == 19


# ── FK constraint ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_runbooks_fk_satisfied(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    # Verify FK constraint is not violated by querying with JOIN
    rows = await _fetch_all(
        db_path,
        """
        SELECT r.id FROM runbooks r
        LEFT JOIN services s ON r.service_name = s.name
        WHERE s.name IS NULL
        """,
    )
    assert rows == [], f"Orphaned runbooks (FK violation): {rows}"


# ── episodic_incidents table ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_creates_episodic_incidents_table(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    rows = await _fetch_all(
        db_path,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='episodic_incidents'",
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_seed_episodic_incidents_starts_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await seed(db_path, verbose=False)
    rows = await _fetch_all(db_path, "SELECT COUNT(*) as cnt FROM episodic_incidents")
    assert rows[0]["cnt"] == 0
