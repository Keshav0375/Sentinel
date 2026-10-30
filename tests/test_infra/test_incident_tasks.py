"""Tests for sentinel.infra.incident_tasks — the in-flight incident registry and its drain."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from sentinel.infra import incident_tasks as tasks_module
from sentinel.infra.incident_tasks import IncidentCapacityError, IncidentTasks


async def _returns() -> None:
    return None


async def test_start_names_and_releases_the_task() -> None:
    tasks = IncidentTasks()

    task = tasks.start("i1", _returns())
    assert task.get_name() == "incident-i1"
    assert len(tasks) == 1 and list(tasks) == [task]
    await task
    await asyncio.sleep(0)

    assert len(tasks) == 0


async def test_at_capacity() -> None:
    tasks = IncidentTasks(2)
    release = asyncio.Event()

    async def blocked() -> None:
        await release.wait()

    first, second = tasks.start("i1", blocked()), tasks.start("i2", blocked())
    assert tasks.at_capacity is True
    assert tasks.max_concurrent == 2

    release.set()
    await asyncio.gather(first, second)
    await asyncio.sleep(0)

    assert tasks.at_capacity is False


def test_rejects_max_below_one() -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        IncidentTasks(0)


async def test_failed_task_is_logged_with_incident_id(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = MagicMock()
    monkeypatch.setattr(tasks_module, "logger", logger)
    tasks = IncidentTasks()

    async def fails() -> None:
        raise RuntimeError("boom")

    task = tasks.start("i1", fails())
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    logger.error.assert_called_once()
    assert logger.error.call_args.args[0] == "incident_task_failed"
    kwargs = logger.error.call_args.kwargs
    assert kwargs["incident_id"] == "i1"
    assert kwargs["error"] == "boom"
    assert kwargs["error_type"] == "RuntimeError"


async def test_shutdown_waits_for_tasks_within_grace() -> None:
    tasks = IncidentTasks()
    done: list[str] = []

    async def slow() -> None:
        await asyncio.sleep(0.05)
        done.append("done")

    tasks.start("i1", slow())

    assert await tasks.shutdown(1.0) == []
    assert done == ["done"]
    assert tasks.closing is True


async def test_shutdown_logs_then_cancels_past_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[tuple[str, str]] = []
    logger = MagicMock()
    logger.error.side_effect = lambda event, **kw: order.append(("log", kw["incident_id"]))
    monkeypatch.setattr(tasks_module, "logger", logger)
    tasks = IncidentTasks()

    async def stuck() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            order.append(("cancelled", "i1"))
            raise

    tasks.start("i1", stuck())

    assert await tasks.shutdown(0.05) == ["i1"]
    assert order == [("log", "i1"), ("cancelled", "i1")]
    assert "incident_task_cancelled" not in [c.args[0] for c in logger.warning.call_args_list]
    assert len(tasks) == 0


async def test_start_after_shutdown_raises() -> None:
    tasks = IncidentTasks()
    await tasks.shutdown(0)
    coro = _returns()

    with pytest.raises(RuntimeError, match="shutting down"):
        tasks.start("i2", coro)

    assert coro.cr_frame is None  # closed, so never "awaited"


async def test_start_enforces_the_cap_itself() -> None:
    tasks = IncidentTasks(1)
    gate = asyncio.Event()
    first = tasks.start("i1", gate.wait())  # type: ignore[arg-type]
    coro = _returns()

    with pytest.raises(IncidentCapacityError, match="at capacity"):
        tasks.start("i2", coro)

    assert coro.cr_frame is None  # closed, never "awaited"
    assert len(tasks) == 1
    gate.set()
    await first


async def test_reserved_slot_counts_until_started_or_released() -> None:
    tasks = IncidentTasks(2)
    tasks.reserve("r1")
    tasks.reserve("r1")  # idempotent: still one slot
    gate = asyncio.Event()
    other = tasks.start("i1", gate.wait())  # type: ignore[arg-type]
    assert tasks.at_capacity and tasks.has("r1") and len(tasks) == 1

    with pytest.raises(IncidentCapacityError):
        tasks.reserve("r2")
    with pytest.raises(IncidentCapacityError):
        tasks.start("i2", _returns())

    started = tasks.start("r1", _returns())  # the reservation is its slot
    await started
    await asyncio.sleep(0)
    assert not tasks.at_capacity and not tasks.has("r1")

    tasks.reserve("r3")
    tasks.release("r3")
    tasks.release("never-reserved")  # a no-op
    assert not tasks.has("r3") and not tasks.at_capacity
    gate.set()
    await other


async def test_reserve_and_reserved_start_refused_after_shutdown() -> None:
    tasks = IncidentTasks()
    tasks.reserve("r1")
    await tasks.shutdown(0)

    with pytest.raises(RuntimeError, match="shutting down"):
        tasks.reserve("r2")
    coro = _returns()
    with pytest.raises(RuntimeError, match="shutting down"):
        tasks.start("r1", coro)
    assert coro.cr_frame is None
    assert not tasks.has("r1")  # the reservation was released
