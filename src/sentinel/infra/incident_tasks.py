"""In-flight incident pipeline tasks (§4.5).

Holds a strong reference to each running incident task, enforces the concurrency cap the
webhook answers with 429, logs a failed task with its ``incident_id``, and runs the
shutdown drain the lifespan performs before it closes the pool (an incident that has to be
cancelled is logged first).
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


class IncidentTasks:
    """The in-flight incident tasks: id map, concurrency cap and shutdown drain."""

    def __init__(self, max_concurrent: int = PIPELINE_MAX_CONCURRENT) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._max = max_concurrent
        self._tasks: dict[asyncio.Task[None], str] = {}
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
        """True when the in-flight count has reached ``max_concurrent``."""
        return len(self._tasks) >= self._max

    def __len__(self) -> int:
        return len(self._tasks)

    def __iter__(self) -> Iterator[asyncio.Task[None]]:
        return iter(list(self._tasks))

    def start(self, incident_id: str, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run *coro* as the task of *incident_id*; kept until it ends.

        Raises:
            RuntimeError: Shutdown began. *coro* is closed unawaited.
        """
        if self._closing:
            coro.close()
            raise RuntimeError("incident tasks are shutting down")
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
