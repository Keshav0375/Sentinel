"""Semantic memory — the §5.3 ``services`` table: service registry, dependencies, runbooks.

Lookup by name is an exact ``WHERE name = $1``. Symptom search ("which service matches
this symptom?") is ``ORDER BY embedding <=> $1`` (cosine distance) in PostgreSQL, served
by the ``idx_services_embedding`` IVFFlat index.

Mapping (decision 2026-10-12, "Phase-1 models ↔ §5.3 schema", item 4):
``ServiceMetadata.oncall_channel``, ``description`` and ``repo_url`` come from the
``metadata`` JSONB keys; ``repo_url`` is ``https://github.com/<metadata.repo>`` when only
``repo`` is present. Runbooks have no table of their own: each service holds one markdown
runbook in ``services.runbook``, read back as a :class:`Runbook` whose steps are the
markdown's numbered or bulleted items (the whole text as one step if it has none).

With no pool (no ``DATABASE_URL``) every lookup raises
:class:`~sentinel.exceptions.SentinelMemoryError` — there are no silent empty results. A
service that is not in the table is still :class:`KeyError`, which callers rely on.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

# asyncpg ships no type stubs (see infra/database.py); only its types are named here.
import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from pgvector import Vector

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra import database
from sentinel.memory.base import MemoryStore
from sentinel.models.memory import Runbook, SemanticRecord
from sentinel.models.service import ServiceMetadata, ServiceTier

# IVFFlat only searches `probes` of the index's lists; at the default of 1 a small or
# freshly built table can miss its nearest rows. Probing every list (= lists of
# idx_services_embedding, migration 001) makes the search exact.
_IVFFLAT_PROBES = 5

_NO_POOL = "memory unavailable: no database configured (DATABASE_URL is not set)"

# Echoed as Runbook.failure_class when the caller names none (query / get_all_runbooks).
_GENERAL_FAILURE_CLASS = "general"

_COLUMNS = "name, team, tier, dependencies, runbook, metadata"

# A markdown list item: "1. step", "2) step", "- step", "* step", "+ step".
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>\S.*)$")


class SemanticMemory:
    """Read-only semantic memory over the PostgreSQL ``services`` table.

    Satisfies the MemoryStore protocol. Implements domain-specific query methods on
    top of the four protocol primitives:

    - get_service(name)              → ServiceMetadata (team, tier, deps, oncall)
    - get_dependencies(name)         → list[str] (downstream service names)
    - get_runbook(svc, class)        → Runbook | None (ordered remediation steps)
    - search_services(embedding, k)  → [(ServiceMetadata, similarity)], nearest first

    Name lookups are cached in-process after the first load so a single incident does
    not hammer the DB with repeated identical queries. The cache lives for the lifetime
    of this object. ``pool`` is None when no database is configured; the object still
    constructs, holds no cache, and every lookup raises.
    """

    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self._pool = pool
        # No cache without a pool, so a cache hit can never hide "memory unavailable".
        self._service_cache: dict[str, ServiceMetadata] | None = None if pool is None else {}
        self._runbook_cache: dict[str, str | None] | None = None if pool is None else {}

    # ── Domain-specific API ───────────────────────────────────────────────────

    async def get_service(self, name: str) -> ServiceMetadata:
        """Return service metadata for the named service.

        Raises:
            KeyError: If the service is not in the database.
            SentinelMemoryError: no database, or the query failed.
        """
        self._require_pool()
        cached = self._cached_service(name)
        if cached is not None:
            return cached
        await self._load(name)
        service = self._cached_service(name)
        if service is None:
            raise KeyError(f"Service not found in semantic memory: {name!r}")
        return service

    async def get_dependencies(self, name: str) -> list[str]:
        """Return the list of services that name depends on.

        Raises:
            KeyError: If the service is not in the database.
            SentinelMemoryError: no database, or the query failed.
        """
        svc = await self.get_service(name)
        return list(svc.dependencies)

    async def get_runbook(self, service: str, failure_class: str) -> Runbook | None:
        """Return the service's runbook, labelled with the requested failure class.

        A service has one runbook (``services.runbook``); *failure_class* does not
        select it, only echoes into ``Runbook.failure_class``. Returns None when the
        service is unknown or has no runbook — callers must handle that gracefully.

        Raises:
            SentinelMemoryError: no database, or the query failed.
        """
        text = await self._runbook_text(service)
        if text is None:
            return None
        return _to_runbook(service, failure_class or _GENERAL_FAILURE_CLASS, text)

    async def get_all_runbooks(self, service: str) -> list[Runbook]:
        """Return every runbook for a service: its one runbook, or [] if it has none.

        Raises:
            SentinelMemoryError: no database, or the query failed.
        """
        runbook = await self.get_runbook(service, _GENERAL_FAILURE_CLASS)
        return [] if runbook is None else [runbook]

    async def search_services(
        self, embedding: list[float], *, top_k: int = 5
    ) -> list[tuple[ServiceMetadata, float]]:
        """Return the services nearest a symptom embedding, most similar first.

        ``ORDER BY embedding <=> $1 LIMIT $2`` inside a transaction that first sets
        ``ivfflat.probes``; similarity is ``1 - cosine distance``. Services without an
        embedding, or whose distance is undefined (a zero vector), are left out.

        Args:
            embedding: Query vector — 384 floats for the §5.3 schema.
            top_k: Maximum number of results (>= 1).

        Raises:
            SentinelMemoryError: no database, or the query failed (e.g. wrong dimensions).
            ValueError: top_k < 1.
        """
        pool = self._require_pool()
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1, got {top_k}")

        async with database.acquire(pool) as conn, conn.transaction():
            await conn.execute(f"SET LOCAL ivfflat.probes = {_IVFFLAT_PROBES}")
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS}, embedding <=> $1 AS distance
                FROM services
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1
                LIMIT $2
                """,
                Vector(embedding),
                top_k,
            )

        results: list[tuple[ServiceMetadata, float]] = []
        for row in rows:
            distance = float(row["distance"])
            if math.isnan(distance):
                continue
            results.append((_row_to_service(row), 1.0 - distance))
        return results

    # ── MemoryStore protocol ──────────────────────────────────────────────────

    async def store(self, record: Any) -> None:
        """Not supported — semantic memory is read-only (seeded by alembic migrations)."""
        raise NotImplementedError(
            "SemanticMemory is read-only. The services table is seeded by alembic migrations."
        )

    async def query(self, query_input: Any, *, top_k: int = 5) -> SemanticRecord:
        """Return a SemanticRecord for the service named by query_input.

        Args:
            query_input: Service name as a string.
            top_k: Unused for semantic memory (an exact name lookup, no ranking).

        Returns:
            SemanticRecord with ServiceMetadata + the service's runbook (if any).

        Raises:
            KeyError: If the service is not in the database.
            SentinelMemoryError: no database, or the query failed.
        """
        name = str(query_input)
        service = await self.get_service(name)
        runbooks = await self.get_all_runbooks(name)
        return SemanticRecord(service=service, runbooks=runbooks)

    async def get(self, record_id: str) -> ServiceMetadata | None:
        """Return ServiceMetadata by service name, or None if not found.

        Raises:
            SentinelMemoryError: no database, or the query failed.
        """
        try:
            return await self.get_service(record_id)
        except KeyError:
            return None

    async def delete(self, record_id: str) -> None:
        """Not supported — semantic memory is read-only."""
        raise NotImplementedError(
            "SemanticMemory is read-only. The services table is seeded by alembic migrations."
        )

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def service_cache_size(self) -> int:
        """Number of services currently cached in memory."""
        return len(self._service_cache or {})

    @property
    def runbook_cache_size(self) -> int:
        """Number of services whose runbook (or its absence) is cached."""
        return len(self._runbook_cache or {})

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise SentinelMemoryError(_NO_POOL)
        return self._pool

    def _cached_service(self, name: str) -> ServiceMetadata | None:
        return None if self._service_cache is None else self._service_cache.get(name)

    async def _runbook_text(self, service: str) -> str | None:
        """The service's runbook markdown; None if the service is unknown or has none."""
        self._require_pool()
        if self._runbook_cache is not None and service in self._runbook_cache:
            return self._runbook_cache[service]
        await self._load(service)
        return None if self._runbook_cache is None else self._runbook_cache.get(service)

    async def _load(self, name: str) -> None:
        """Fetch one ``services`` row by exact name into both caches.

        A missing service is not cached, so a service added later is found.
        """
        pool = self._require_pool()
        async with database.acquire(pool) as conn:
            row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM services WHERE name = $1", name)
        if row is None or self._service_cache is None or self._runbook_cache is None:
            return
        self._service_cache[name] = _row_to_service(row)
        runbook: str | None = row["runbook"]
        self._runbook_cache[name] = runbook if runbook and runbook.strip() else None


# ── Module-level helpers ──────────────────────────────────────────────────────


def _json(raw: str | None) -> Any:
    """A JSONB column as Python (asyncpg returns JSONB as text); None stays None."""
    return None if raw is None else json.loads(raw)


def _row_to_service(row: asyncpg.Record) -> ServiceMetadata:
    """Convert a ``services`` row to ServiceMetadata (decision 2026-10-12, item 4).

    Raises:
        SentinelMemoryError: the row's tier is not one of the §5.3 values.
    """
    name: str = row["name"]
    try:
        tier = ServiceTier(row["tier"])
    except ValueError:
        raise SentinelMemoryError(
            f"service {name!r} has an invalid tier {row['tier']!r} in the services table"
        ) from None

    loaded_meta = _json(row["metadata"])
    metadata: dict[str, Any] = loaded_meta if isinstance(loaded_meta, dict) else {}  # pyright: ignore[reportUnknownVariableType]
    loaded_deps = _json(row["dependencies"])
    dependencies: list[Any] = loaded_deps if isinstance(loaded_deps, list) else []  # pyright: ignore[reportUnknownVariableType]

    repo_url = _str_or_none(metadata.get("repo_url"))
    repo = _str_or_none(metadata.get("repo"))
    if repo_url is None and repo is not None:
        repo_url = f"https://github.com/{repo}"

    return ServiceMetadata(
        name=name,
        team=row["team"] or "",
        tier=tier,
        oncall_channel=_str_or_none(metadata.get("oncall_channel")),
        repo_url=repo_url,
        description=_str_or_none(metadata.get("description")) or "",
        dependencies=[str(dep) for dep in dependencies],
    )


def _str_or_none(value: Any) -> str | None:
    """A non-blank string, else None."""
    return value if isinstance(value, str) and value.strip() else None


def _to_runbook(service: str, failure_class: str, text: str) -> Runbook:
    return Runbook(
        id=f"{service}-runbook",
        service_name=service,
        failure_class=failure_class,
        steps=_parse_runbook_steps(text),
    )


def _parse_runbook_steps(markdown: str) -> list[str]:
    """The ordered steps of a markdown runbook.

    Each numbered (``1.``, ``2)``) or bulleted (``-``, ``*``, ``+``) item is one step,
    its marker stripped; indented lines directly under an item continue it. A runbook
    with no list items is a single step: its whole (stripped) text.
    """
    items: list[list[str]] = []
    in_item = False
    for line in markdown.splitlines():
        item = _LIST_ITEM.match(line)
        if item is not None:
            items.append([item.group("text").strip()])
            in_item = True
        elif in_item and line.strip() and line[:1].isspace():
            items[-1].append(line.strip())
        else:
            in_item = False
    return [" ".join(parts) for parts in items] if items else [markdown.strip()]


# Runtime check: assert SemanticMemory satisfies MemoryStore at import time
# so any signature drift is caught immediately rather than at first use.
assert isinstance(SemanticMemory.__new__(SemanticMemory), MemoryStore), (
    "SemanticMemory does not satisfy the MemoryStore protocol"
)
