"""SSE streaming endpoint.

Endpoints:
    GET  /events/{incident_id}          — SSE stream of ``PipelineEvent`` objects.

The SSE endpoint subscribes to the ``EventBus`` and yields one ``data:`` line per
``PipelineEvent``.  A ``None`` sentinel from ``EventBus.close_incident`` closes the
stream cleanly.

There is no approval endpoint: the revert PR on Sentinel-deployment is the only HITL
gate (backend.md §3.3) — a human merges or closes it on GitHub.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from sentinel.infra.event_bus import EventBus

# ── Router factory ────────────────────────────────────────────────────────────


def make_events_router(event_bus: EventBus) -> APIRouter:
    """Create the events router with the SSE endpoint.

    Args:
        event_bus: Shared ``EventBus`` instance (same one used by the pipeline).

    Returns:
        ``APIRouter`` with ``GET /events/{incident_id}`` — the SSE stream.
    """
    router = APIRouter()

    @router.get("/events/{incident_id}")
    async def stream_events(  # pyright: ignore[reportUnusedFunction]
        incident_id: str,
    ) -> StreamingResponse:
        """Stream pipeline events for *incident_id* as Server-Sent Events.

        Each event is emitted as::

            data: {"type": "agent_started", "agent_name": "...", ...}\n\n

        The stream ends when ``EventBus.close_incident`` sends the ``None``
        sentinel (i.e. when the pipeline finishes or is cancelled).

        Returns:
            ``StreamingResponse`` with ``Content-Type: text/event-stream``.
        """

        async def _generate() -> AsyncGenerator[str, None]:
            q = await event_bus.subscribe(incident_id)
            try:
                while True:
                    event = await q.get()
                    if event is None:
                        break
                    yield f"data: {event.model_dump_json()}\n\n"
            finally:
                await event_bus.unsubscribe(incident_id, q)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
