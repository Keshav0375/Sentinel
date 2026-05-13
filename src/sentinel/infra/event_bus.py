"""SSE event bus for real-time pipeline updates.

Provides a pub/sub mechanism for streaming pipeline events to connected
clients. Each client gets its own ``asyncio.Queue`` — the bus fans out
every published event to all subscribers for a given incident.

Usage::

    bus = EventBus()

    # Publisher (orchestrator / tracer):
    await bus.publish("inc-001", PipelineEvent(type=EventType.AGENT_STARTED, ...))

    # Consumer (SSE endpoint):
    async for event in bus.subscribe("inc-001"):
        yield event.model_dump_json()
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """Pipeline event types streamed to connected clients."""

    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    HITL_REQUESTED = "hitl_requested"
    HITL_RESOLVED = "hitl_resolved"
    INCIDENT_RESOLVED = "incident_resolved"


class PipelineEvent(BaseModel):
    """A single event emitted during pipeline execution.

    Streamed to connected SSE clients via the EventBus.
    """

    type: EventType
    agent_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    incident_id: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class EventBus:
    """Fan-out event bus — one asyncio.Queue per subscriber per incident.

    Thread-safe for asyncio: all operations run on a single event loop.
    Subscribers receive a sentinel ``None`` when the incident completes
    so they can cleanly exit their iteration loop.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[PipelineEvent | None]]] = (
            defaultdict(list)
        )

    async def publish(self, incident_id: str, event: PipelineEvent) -> int:
        """Fan out *event* to every subscriber for *incident_id*.

        Returns the number of subscribers that received the event.
        """
        queues = self._subscribers.get(incident_id, [])
        for q in queues:
            await q.put(event)
        return len(queues)

    async def subscribe(
        self,
        incident_id: str,
        *,
        max_size: int = 256,
    ) -> asyncio.Queue[PipelineEvent | None]:
        """Register a new subscriber for *incident_id*.

        Returns a queue that will receive every ``PipelineEvent`` published
        for this incident. A ``None`` sentinel signals end-of-stream.
        """
        q: asyncio.Queue[PipelineEvent | None] = asyncio.Queue(maxsize=max_size)
        self._subscribers[incident_id].append(q)
        return q

    async def unsubscribe(
        self,
        incident_id: str,
        queue: asyncio.Queue[PipelineEvent | None],
    ) -> None:
        """Remove a subscriber queue for *incident_id*."""
        queues = self._subscribers.get(incident_id, [])
        try:
            queues.remove(queue)
        except ValueError:
            pass
        if not queues:
            self._subscribers.pop(incident_id, None)

    async def close_incident(self, incident_id: str) -> None:
        """Signal all subscribers that *incident_id* is done.

        Sends ``None`` to every queue and removes all subscribers.
        """
        queues = self._subscribers.pop(incident_id, [])
        for q in queues:
            await q.put(None)

    @property
    def subscriber_count(self) -> int:
        """Total number of active subscriber queues across all incidents."""
        return sum(len(qs) for qs in self._subscribers.values())

    def incident_subscriber_count(self, incident_id: str) -> int:
        """Number of active subscribers for a specific incident."""
        return len(self._subscribers.get(incident_id, []))
