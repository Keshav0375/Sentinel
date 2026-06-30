"""Semantic memory — SQLite service map, dependency graph, and runbooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sentinel.infra.db import get_db
from sentinel.memory.base import MemoryStore
from sentinel.models.memory import Runbook, SemanticRecord
from sentinel.models.service import ServiceMetadata


class SemanticMemory:
    """Read-only semantic memory backed by the SQLite services and runbooks tables.

    Satisfies the MemoryStore protocol. Implements three domain-specific query
    methods on top of the four protocol primitives:

    - get_service(name)       → ServiceMetadata (team, tier, deps, oncall)
    - get_dependencies(name)  → list[str] (downstream service names)
    - get_runbook(svc, class) → Runbook | None (ordered remediation steps)

    Results are cached in-process after the first load so a single incident
    does not hammer the DB with repeated identical queries. The cache lives
    for the lifetime of this object — create a new instance per incident to
    start fresh.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._service_cache: dict[str, ServiceMetadata] = {}
        self._runbook_cache: dict[tuple[str, str], Runbook | None] = {}

    # ── Domain-specific API ───────────────────────────────────────────────────

    async def get_service(self, name: str) -> ServiceMetadata:
        """Return service metadata for the named service.

        Raises:
            KeyError: If the service is not in the database.
        """
        if name not in self._service_cache:
            async with get_db(self._db_path) as conn:
                async with conn.execute("SELECT * FROM services WHERE name = ?", (name,)) as cur:
                    row = await cur.fetchone()
            if row is None:
                raise KeyError(f"Service not found in semantic memory: {name!r}")
            self._service_cache[name] = ServiceMetadata(
                name=row["name"],
                team=row["team"],
                tier=row["tier"],
                oncall_channel=row["oncall_channel"],
                repo_url=row["repo_url"],
                description=row["description"] or "",
                dependencies=json.loads(row["dependencies"] or "[]"),
            )
        return self._service_cache[name]

    async def get_dependencies(self, name: str) -> list[str]:
        """Return the list of services that name depends on.

        Raises:
            KeyError: If the service is not in the database.
        """
        svc = await self.get_service(name)
        return list(svc.dependencies)

    async def get_runbook(self, service: str, failure_class: str) -> Runbook | None:
        """Return the runbook for the given (service, failure_class) pair.

        Returns None if no runbook exists for that combination — callers must
        handle the no-runbook case gracefully.
        """
        key = (service, failure_class)
        if key not in self._runbook_cache:
            async with get_db(self._db_path) as conn:
                async with conn.execute(
                    """
                    SELECT * FROM runbooks
                    WHERE service_name = ? AND failure_class = ?
                    """,
                    (service, failure_class),
                ) as cur:
                    row = await cur.fetchone()
            self._runbook_cache[key] = (
                None
                if row is None
                else Runbook(
                    id=row["id"],
                    service_name=row["service_name"],
                    failure_class=row["failure_class"],
                    steps=json.loads(row["steps"]),
                )
            )
        return self._runbook_cache[key]

    async def get_all_runbooks(self, service: str) -> list[Runbook]:
        """Return all runbooks for a service across all failure classes."""
        async with get_db(self._db_path) as conn:
            async with conn.execute(
                "SELECT * FROM runbooks WHERE service_name = ?", (service,)
            ) as cur:
                rows = await cur.fetchall()
        runbooks: list[Runbook] = []
        for row in rows:
            rb = Runbook(
                id=row["id"],
                service_name=row["service_name"],
                failure_class=row["failure_class"],
                steps=json.loads(row["steps"]),
            )
            self._runbook_cache[(service, rb.failure_class)] = rb
            runbooks.append(rb)
        return runbooks

    # ── MemoryStore protocol ──────────────────────────────────────────────────

    async def store(self, record: Any) -> None:
        """Not supported — semantic memory is read-only (seeded by data/seed.py)."""
        raise NotImplementedError(
            "SemanticMemory is read-only. Use data/seed.py to populate the DB."
        )

    async def query(self, query_input: Any, *, top_k: int = 5) -> SemanticRecord:
        """Return a SemanticRecord for the service named by query_input.

        Args:
            query_input: Service name as a string.
            top_k: Unused for semantic memory (no similarity ranking needed).

        Returns:
            SemanticRecord with ServiceMetadata + all runbooks for that service.
        """
        name = str(query_input)
        service = await self.get_service(name)
        runbooks = await self.get_all_runbooks(name)
        return SemanticRecord(service=service, runbooks=runbooks)

    async def get(self, record_id: str) -> ServiceMetadata | None:
        """Return ServiceMetadata by service name, or None if not found."""
        try:
            return await self.get_service(record_id)
        except KeyError:
            return None

    async def delete(self, record_id: str) -> None:
        """Not supported — semantic memory is read-only."""
        raise NotImplementedError(
            "SemanticMemory is read-only. Use data/seed.py to populate the DB."
        )

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def service_cache_size(self) -> int:
        """Number of services currently cached in memory."""
        return len(self._service_cache)

    @property
    def runbook_cache_size(self) -> int:
        """Number of (service, failure_class) pairs currently cached."""
        return len(self._runbook_cache)


# Runtime check: assert SemanticMemory satisfies MemoryStore at import time
# so any signature drift is caught immediately rather than at first use.
assert isinstance(SemanticMemory.__new__(SemanticMemory), MemoryStore), (
    "SemanticMemory does not satisfy the MemoryStore protocol"
)
