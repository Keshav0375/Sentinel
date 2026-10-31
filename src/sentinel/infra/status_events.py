"""Incident status events on Redis pub/sub (decision 2026-10-25 D).

``PUBLISH sentinel:events`` one JSON message per status change of an incident —
``{"v": 1, "incident_id", "status", "at"}`` — when it is accepted (``in_progress``), resumed
after a restart (``resumed``) and when it ends (``rollback_proposed`` | ``escalated``).

Publish only: no consumer exists yet (the ops dashboard is a static site that reads its
snapshots, not this channel). Each publish is a fire-and-forget background task bounded by
:data:`STATUS_EVENT_TIMEOUT_S`; a failure is logged (``status_event_failed``), never raised.
Without Redis every publish is a no-op.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from sentinel.infra.logging import get_logger
from sentinel.infra.redis_client import RedisHandle

__all__ = ["STATUS_CHANNEL", "STATUS_EVENT_TIMEOUT_S", "StatusPublisher"]

logger = get_logger("sentinel.infra.status_events")

STATUS_CHANNEL: Final = "sentinel:events"
# One publish, connect included, in seconds.
STATUS_EVENT_TIMEOUT_S: Final = 1.0
_EVENT_VERSION: Final = 1


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StatusPublisher:
    """Publishes incident status events in the background; a no-op without Redis."""

    def __init__(
        self, redis: RedisHandle | None, *, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self._redis = redis
        self._clock = clock
        self._tasks: set[asyncio.Task[None]] = set()

    def publish(self, incident_id: str, status: str) -> None:
        """Publish *status* for *incident_id*; returns at once, never raises."""
        if self._redis is None:
            return
        message = json.dumps(
            {
                "v": _EVENT_VERSION,
                "incident_id": incident_id,
                "status": status,
                "at": self._clock().isoformat(),
            }
        )
        task = asyncio.create_task(
            self._send(self._redis, incident_id, status, message),
            name=f"status-event-{incident_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, grace_s: float) -> None:
        """Wait up to *grace_s* for the pending publishes, then cancel the rest."""
        pending = set(self._tasks)
        if not pending:
            return
        _, left = await asyncio.wait(pending, timeout=max(grace_s, 0.0))
        for task in left:
            task.cancel()
        if left:
            await asyncio.wait(left, timeout=STATUS_EVENT_TIMEOUT_S)

    @staticmethod
    async def _send(redis: RedisHandle, incident_id: str, status: str, message: str) -> None:
        try:
            async with asyncio.timeout(STATUS_EVENT_TIMEOUT_S):
                client = await redis.client()
                await client.publish(STATUS_CHANNEL, message)
        except Exception as exc:
            logger.warning(
                "status_event_failed",
                incident_id=incident_id,
                status=status,
                error_type=type(exc).__name__,
            )
