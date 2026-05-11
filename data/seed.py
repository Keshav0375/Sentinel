"""Seed script — creates SQLite schema and loads service map + runbooks.

Run from the project root::

    python -m data.seed                        # uses ./data/sentinel.db
    python -m data.seed ./data/my_test.db      # custom path
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from sentinel.infra.db import create_tables, get_db

# Paths are resolved relative to this file so the script works regardless
# of the working directory it is launched from.
_DATA_DIR = Path(__file__).parent
_SERVICES_DIR = _DATA_DIR / "services"


async def seed(db_path: Path, *, verbose: bool = True) -> tuple[int, int]:
    """Create tables and load all seed data into the SQLite database.

    Idempotent — uses INSERT OR REPLACE so re-running never fails or
    duplicates rows. Services are inserted before runbooks to satisfy
    the FK constraint on runbooks.service_name.

    Args:
        db_path: Path to the SQLite database file. Parent directories are
            created automatically by create_tables().
        verbose: When True, print a summary line after seeding.

    Returns:
        Tuple of (services_inserted, runbooks_inserted).
    """
    await create_tables(db_path)

    service_map = json.loads(
        (_SERVICES_DIR / "service_map.json").read_text(encoding="utf-8")
    )
    runbooks_data = json.loads(
        (_SERVICES_DIR / "runbooks.json").read_text(encoding="utf-8")
    )

    services: list[dict] = service_map["services"]  # type: ignore[assignment]
    runbooks: list[dict] = runbooks_data["runbooks"]  # type: ignore[assignment]

    async with get_db(db_path) as conn:
        # Services first — runbooks have a FK on service_name
        for svc in services:
            await conn.execute(
                """
                INSERT OR REPLACE INTO services
                    (name, team, tier, oncall_channel, repo_url, description, dependencies)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    svc["name"],
                    svc["team"],
                    svc["tier"],
                    svc.get("oncall_channel"),
                    svc.get("repo_url"),
                    svc.get("description"),
                    json.dumps(svc.get("dependencies", [])),
                ),
            )

        for rb in runbooks:
            await conn.execute(
                """
                INSERT OR REPLACE INTO runbooks
                    (id, service_name, failure_class, steps)
                VALUES (?, ?, ?, ?)
                """,
                (
                    rb["id"],
                    rb["service_name"],
                    rb["failure_class"],
                    json.dumps(rb["steps"]),
                ),
            )

        await conn.commit()

    if verbose:
        print(
            f"Seeded {len(services)} services and {len(runbooks)} runbooks "
            f"into {db_path}"
        )

    return len(services), len(runbooks)


if __name__ == "__main__":
    _db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./data/sentinel.db")
    asyncio.run(seed(_db_path))
