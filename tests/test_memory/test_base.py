"""Tests for sentinel.memory.base — MemoryStore protocol."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from sentinel.memory.base import MemoryStore

# ── Conforming implementation ─────────────────────────────────────────────────


class _ConformingStore:
    """Minimal async implementation that structurally satisfies MemoryStore."""

    async def store(self, record: Any) -> None:
        pass

    async def query(self, query_input: Any, *, top_k: int = 5) -> Any:
        return []

    async def get(self, record_id: str) -> Any:
        return None

    async def delete(self, record_id: str) -> None:
        pass


class _MissingDeleteStore:
    """Missing delete() — should NOT satisfy the protocol."""

    async def store(self, record: Any) -> None:
        pass

    async def query(self, query_input: Any, *, top_k: int = 5) -> Any:
        return []

    async def get(self, record_id: str) -> Any:
        return None


class _SyncStore:
    """Sync methods — structurally present but not async."""

    def store(self, record: Any) -> None:  # type: ignore[override]
        pass

    def query(self, query_input: Any, *, top_k: int = 5) -> Any:  # type: ignore[override]
        return []

    def get(self, record_id: str) -> Any:  # type: ignore[override]
        return None

    def delete(self, record_id: str) -> None:  # type: ignore[override]
        pass


# ── Protocol structure ────────────────────────────────────────────────────────


def test_memory_store_is_runtime_checkable() -> None:
    store = _ConformingStore()
    assert isinstance(store, MemoryStore)


def test_missing_method_fails_protocol_check() -> None:
    store = _MissingDeleteStore()
    assert not isinstance(store, MemoryStore)


def test_protocol_has_four_methods() -> None:
    required = {"store", "query", "get", "delete"}
    members = {
        name
        for name, _ in inspect.getmembers(MemoryStore, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert required.issubset(members), f"Missing methods: {required - members}"


def test_protocol_methods_are_coroutines() -> None:
    store = _ConformingStore()
    assert inspect.iscoroutinefunction(store.store)
    assert inspect.iscoroutinefunction(store.query)
    assert inspect.iscoroutinefunction(store.get)
    assert inspect.iscoroutinefunction(store.delete)


def test_sync_store_passes_runtime_check() -> None:
    # runtime_checkable only checks attribute presence, not whether the method
    # is async — this is a known limitation of Protocol at runtime.
    # Static type checkers (pyright) catch async/sync mismatches.
    store = _SyncStore()
    assert isinstance(store, MemoryStore)


# ── Method signatures ─────────────────────────────────────────────────────────


def test_query_has_top_k_parameter() -> None:
    sig = inspect.signature(_ConformingStore.query)
    assert "top_k" in sig.parameters
    assert sig.parameters["top_k"].default == 5


def test_store_accepts_record_param() -> None:
    sig = inspect.signature(_ConformingStore.store)
    assert "record" in sig.parameters


def test_get_accepts_record_id_param() -> None:
    sig = inspect.signature(_ConformingStore.get)
    assert "record_id" in sig.parameters


def test_delete_accepts_record_id_param() -> None:
    sig = inspect.signature(_ConformingStore.delete)
    assert "record_id" in sig.parameters


# ── Async contract ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conforming_store_methods_are_awaitable() -> None:
    store = _ConformingStore()
    await store.store("any-record")
    result = await store.query("query", top_k=3)
    assert result == []
    record = await store.get("some-id")
    assert record is None
    await store.delete("some-id")


@pytest.mark.asyncio
async def test_query_default_top_k_is_5() -> None:
    calls: list[int] = []

    class TrackingStore(_ConformingStore):
        async def query(self, query_input: Any, *, top_k: int = 5) -> Any:
            calls.append(top_k)
            return []

    store = TrackingStore()
    await store.query("input")
    assert calls == [5]


# ── Importability ─────────────────────────────────────────────────────────────


def test_memory_store_importable() -> None:
    from sentinel.memory.base import MemoryStore as MS

    assert MS is MemoryStore


def test_plain_object_does_not_satisfy_protocol() -> None:
    assert not isinstance(object(), MemoryStore)


def test_none_does_not_satisfy_protocol() -> None:
    assert not isinstance(None, MemoryStore)
