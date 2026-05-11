"""MemoryStore protocol — swappable storage abstraction."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryStore(Protocol):
    """Structural protocol for persistent memory backends.

    Both SemanticMemory (service/runbook lookups) and EpisodicMemory
    (incident similarity search) satisfy this protocol without inheriting
    from it. Concrete implementations can be swapped — e.g. SQLite → Cosmos
    DB in Phase 2 — without changing any caller.

    Method signatures use Any because each concrete implementation has
    domain-specific typed methods on top of these four primitives:

    - EpisodicMemory[EpisodicRecord]: store_incident(), search_similar()
    - SemanticMemory[SemanticRecord]: get_service(), get_runbook()

    All methods are async to stay consistent with the aiosqlite backend and
    any future network-backed store (Cosmos, Redis).
    """

    async def store(self, record: Any) -> None:
        """Persist a record. Upsert semantics — updates if ID already exists."""
        ...

    async def query(self, query_input: Any, *, top_k: int = 5) -> Any:
        """Search the store. Semantics are implementation-specific:

        EpisodicMemory: cosine similarity search; query_input is a
        list[float] embedding vector; returns MemoryQueryResult.

        SemanticMemory: structured lookup by service name or failure class;
        returns SemanticRecord or list[Runbook].
        """
        ...

    async def get(self, record_id: str) -> Any:
        """Return a record by primary key. Implementations return None if absent."""
        ...

    async def delete(self, record_id: str) -> None:
        """Remove a record by primary key. No-op if the record does not exist."""
        ...
