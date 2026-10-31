"""In-flight incident pipeline tasks (§4.5).

Holds a strong reference to each running incident task, enforces the concurrency cap the
webhook answers with 429, logs a failed task with its ``incident_id``, and runs the
shutdown drain the lifespan performs before it closes the pool (an incident that has to be
cancelled is logged first).

The cap (``SENTINEL_MAX_CONCURRENT_PIPELINES``) is enforced HERE, by :meth:`IncidentTasks.start`
itself, not only by the callers' ``at_capacity`` check. A caller that must await between the
check and the start (the resumer writes its marker first) holds a slot with
:meth:`~IncidentTasks.reserve` instead: a reserved slot counts toward the cap until it is
started or released, so no other start can take it in the gap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterator
from typing import Any, Final

from sentinel.config import PIPELINE_MAX_CONCURRENT
from sentinel.infra.logging import get_logger

# After the grace expires and the stragglers are cancelled: how long to let them unwind.
CANCEL_WAIT_S: Final = 2.0

logger = get_logger("sentinel.infra.incident_tasks")


class IncidentCapacityError(RuntimeError):
    """The cap is reached: no slot is free for another incident."""


class IncidentTasks:
    """The in-flight incident tasks: id map, concurrency cap and shutdown drain."""

    def __init__(self, max_concurrent: int = PIPELINE_MAX_CONCURRENT) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._max = max_concurrent
        self._tasks: dict[asyncio.Task[None], str] = {}
        # Slots held (by incident id) between reserve() and start() / release().
        self._reserved: set[str] = set()
        self._closing = False

    @property
    def max_concurrent(self) -> int:
        """The most incidents allowed in flight at once."""
        return self._max

    @property
    def closing(self) -> bool:
        """True once :meth:`shutdown` began: no new incident is accepted."""
        return self._closing

    @property
    def at_capacity(self) -> bool:
        """True when the in-flight tasks plus the reserved slots reach ``max_concurrent``."""
        return len(self._tasks) + len(self._reserved) >= self._max

    def __len__(self) -> int:
        return len(self._tasks)

    def __iter__(self) -> Iterator[asyncio.Task[None]]:
        return iter(list(self._tasks))

    def has(self, incident_id: str) -> bool:
        """True while a task of *incident_id* is in flight or holds a reserved slot."""
        return incident_id in self._reserved or incident_id in self._tasks.values()

    def reserve(self, incident_id: str) -> None:
        """Hold one slot for *incident_id* until :meth:`start` or :meth:`release`.

        Raises:
            RuntimeError: Shutdown began.
            IncidentCapacityError: No slot is free (a reserved one counts as taken).
        """
        if self._closing:
            raise RuntimeError("incident tasks are shutting down")
        if incident_id in self._reserved:
            return
        if self.at_capacity:
            raise IncidentCapacityError(f"at capacity ({self._max} incidents in flight)")
        self._reserved.add(incident_id)

    def release(self, incident_id: str) -> None:
        """Give back the slot :meth:`reserve` held for *incident_id* (a no-op without one)."""
        self._reserved.discard(incident_id)

    def start(self, incident_id: str, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run *coro* as the task of *incident_id*; kept until it ends.

        Enforces the cap itself: *incident_id*'s reserved slot is used when it holds one,
        otherwise a free slot must exist.

        Raises:
            RuntimeError: Shutdown began (the reservation, if any, is released).
            IncidentCapacityError: No reservation and no free slot.
            In both cases *coro* is closed unawaited.
        """
        reserved = incident_id in self._reserved
        if self._closing:
            self._reserved.discard(incident_id)
            coro.close()
            raise RuntimeError("incident tasks are shutting down")
        if not reserved and self.at_capacity:
            coro.close()
            raise IncidentCapacityError(f"at capacity ({self._max} incidents in flight)")
        self._reserved.discard(incident_id)
        task = asyncio.create_task(coro, name=f"incident-{incident_id}")
        self._tasks[task] = incident_id
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[None]) -> None:
        incident_id = self._tasks.pop(task, task.get_name())
        if task.cancelled():
            if not self._closing:
                logger.warning("incident_task_cancelled", incident_id=incident_id)
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "incident_task_failed",
                incident_id=incident_id,
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=exc,
            )

    async def shutdown(self, grace_s: float) -> list[str]:
        """Stop accepting; wait up to *grace_s*; log, then cancel, the rest.

        Returns:
            The ids of the incidents that were cancelled, sorted.
        """
        self._closing = True
        in_flight = list(self._tasks)
        if not in_flight:
            return []
        logger.info("incident_drain_start", in_flight=len(in_flight), grace_s=grace_s)
        done, pending = await asyncio.wait(in_flight, timeout=grace_s)
        cancelled: list[str] = []
        for task in pending:
            incident_id = self._tasks.get(task, task.get_name())
            logger.error("incident_cancelled_on_shutdown", incident_id=incident_id, grace_s=grace_s)
            task.cancel()
            cancelled.append(incident_id)
        if pending:
            await asyncio.wait(pending, timeout=CANCEL_WAIT_S)
        logger.info("incident_drain_done", completed=len(done), cancelled=len(cancelled))
        return sorted(cancelled)
