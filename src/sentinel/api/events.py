"""SSE streaming endpoint and HITL approval API.

Endpoints:
    GET  /events/{incident_id}          — SSE stream of ``PipelineEvent`` objects.
    POST /incidents/{incident_id}/approve — resolve the pending HITL gate.

The SSE endpoint subscribes to the ``EventBus`` and yields one ``data:`` line per
``PipelineEvent``.  A ``None`` sentinel from ``EventBus.close_incident`` closes the
stream cleanly.

The approval endpoint signals the ``HitlGateRegistry``, unblocking the
``request_human_approval`` tool that is awaiting a decision.

Usage (wiring into the pipeline)::

    event_bus = EventBus()
    gate_registry = HitlGateRegistry()
    api_approval_fn = make_api_approval_fn(event_bus, gate_registry)

    # Pass api_approval_fn to build_remediation_agent at startup.
    # Before each Runner.run call, set the incident context:
    token = _CURRENT_INCIDENT_ID.set(incident_id)
    try:
        await Runner.run(orchestrator, ...)
    finally:
        _CURRENT_INCIDENT_ID.reset(token)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sentinel.infra.event_bus import EventBus, EventType, PipelineEvent
from sentinel.infra.logging import get_logger
from sentinel.tools.hitl import ApprovalFn

logger = get_logger("sentinel.api.events")

# ── Incident context propagation ──────────────────────────────────────────────

_CURRENT_INCIDENT_ID: ContextVar[str] = ContextVar("current_incident_id", default="")


def set_current_incident(incident_id: str) -> Token[str]:
    """Set the active incident_id for the current async context.

    Call this at the start of each pipeline run so that the dashboard
    approval_fn can retrieve the incident_id without an extra argument.

    Returns:
        A ``Token`` that can be passed to ``reset_current_incident`` to
        restore the previous value when the pipeline finishes.
    """
    return _CURRENT_INCIDENT_ID.set(incident_id)


def reset_current_incident(token: Token[str]) -> None:
    """Reset the incident context to its previous value.

    Always call this in a ``finally`` block after ``set_current_incident``
    to avoid leaking the incident_id into unrelated coroutines.
    """
    _CURRENT_INCIDENT_ID.reset(token)


# ── HITL gate registry ────────────────────────────────────────────────────────


@dataclass
class HitlGate:
    """State for a single pending HITL approval request.

    The ``event`` is set when a decision arrives via the approval endpoint.
    ``decision`` and ``comment`` are populated before ``event.set()`` so the
    waiting coroutine can read them immediately without a race.
    """

    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: str = "reject"
    comment: str | None = None


class HitlGateRegistry:
    """Maps incident_id → pending HitlGate.

    There is at most one active gate per incident at a time (the remediation
    agent gates are sequential, not concurrent).
    """

    def __init__(self) -> None:
        self._gates: dict[str, HitlGate] = {}

    def create_gate(self, incident_id: str) -> HitlGate:
        """Create and register a new gate for *incident_id*.

        Replaces any existing gate (e.g. a stale one from a crashed pipeline).
        """
        gate = HitlGate()
        self._gates[incident_id] = gate
        logger.debug("hitl_gate_created", incident_id=incident_id)
        return gate

    async def resolve(
        self,
        incident_id: str,
        decision: str,
        comment: str | None = None,
    ) -> None:
        """Record a decision and unblock the waiting approval_fn.

        Args:
            incident_id: The incident whose gate to resolve.
            decision: ``"approve"`` or ``"reject"``.
            comment: Optional reviewer comment.

        Raises:
            KeyError: If there is no active gate for *incident_id*.
        """
        gate = self._gates.get(incident_id)
        if gate is None:
            raise KeyError(incident_id)
        gate.decision = decision
        gate.comment = comment
        gate.event.set()
        logger.info(
            "hitl_gate_resolved",
            incident_id=incident_id,
            decision=decision,
        )

    def get_gate(self, incident_id: str) -> HitlGate | None:
        """Return the active gate for *incident_id*, or ``None`` if absent."""
        return self._gates.get(incident_id)

    def clear(self, incident_id: str) -> None:
        """Remove the gate for *incident_id* (after the decision is consumed)."""
        self._gates.pop(incident_id, None)

    @property
    def active_count(self) -> int:
        """Number of currently pending gates."""
        return len(self._gates)


# ── Dashboard approval function ───────────────────────────────────────────────


def make_api_approval_fn(
    event_bus: EventBus,
    gate_registry: HitlGateRegistry,
) -> ApprovalFn:
    """Create an ``ApprovalFn`` that blocks on the API approval endpoint.

    This replaces the terminal ``input()`` handler when running with the live
    dashboard.  The function:

    1. Reads the current ``incident_id`` from ``_CURRENT_INCIDENT_ID`` (set
       by the pipeline runner before calling ``Runner.run``).
    2. Creates a gate in ``HitlGateRegistry`` for this incident.
    3. Publishes a ``hitl_requested`` event so the dashboard can highlight the
       HITL card and show approve/reject buttons.
    4. Awaits the gate event (set when the dashboard POSTs to the approve
       endpoint).
    5. Publishes a ``hitl_resolved`` event, cleans up the gate, and returns
       the decision to the calling tool.

    If no incident context is set (e.g. in tests or CLI mode), falls back to
    rejecting immediately with a clear error message.
    """

    async def approval_fn(display: str) -> tuple[str, str | None]:
        incident_id = _CURRENT_INCIDENT_ID.get()
        if not incident_id:
            logger.warning("hitl_no_incident_context")
            return "reject", "No incident context — cannot route to dashboard."

        gate = gate_registry.create_gate(incident_id)
        await event_bus.publish(
            incident_id,
            PipelineEvent(
                type=EventType.HITL_REQUESTED,
                agent_name="remediation_agent",
                incident_id=incident_id,
                data={"display": display},
            ),
        )
        logger.info("hitl_waiting_for_dashboard", incident_id=incident_id)

        await gate.event.wait()

        decision = gate.decision
        comment = gate.comment
        gate_registry.clear(incident_id)

        await event_bus.publish(
            incident_id,
            PipelineEvent(
                type=EventType.HITL_RESOLVED,
                agent_name="remediation_agent",
                incident_id=incident_id,
                data={"decision": decision, "comment": comment or ""},
            ),
        )
        logger.info(
            "hitl_resolved_by_dashboard",
            incident_id=incident_id,
            decision=decision,
        )
        return decision, comment

    return approval_fn


# ── API models ────────────────────────────────────────────────────────────────


class ApprovalDecision(BaseModel):
    """Request body for POST /incidents/{incident_id}/approve."""

    action: Literal["approve", "reject"]
    comment: str | None = None


class ApprovalResponse(BaseModel):
    """Response body for POST /incidents/{incident_id}/approve."""

    status: Literal["accepted"]
    decision: str


# ── Router factory ────────────────────────────────────────────────────────────


def make_events_router(
    event_bus: EventBus,
    gate_registry: HitlGateRegistry,
) -> APIRouter:
    """Create the events router with SSE and HITL approval endpoints.

    Args:
        event_bus: Shared ``EventBus`` instance (same one used by the pipeline).
        gate_registry: Shared ``HitlGateRegistry`` instance.

    Returns:
        ``APIRouter`` with the following routes:
        - ``GET /events/{incident_id}`` — SSE stream.
        - ``POST /incidents/{incident_id}/approve`` — HITL resolution.
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

    @router.post(
        "/incidents/{incident_id}/approve",
        status_code=202,
        response_model=ApprovalResponse,
    )
    async def approve_hitl(  # pyright: ignore[reportUnusedFunction]
        incident_id: str,
        body: ApprovalDecision,
    ) -> ApprovalResponse:
        """Resolve the pending HITL gate for *incident_id*.

        The dashboard calls this when the operator clicks Approve or Reject.
        The response is immediate — the actual remediation action happens
        asynchronously in the pipeline after the gate is resolved.

        Raises:
            404: If there is no active HITL gate for *incident_id*.
        """
        if gate_registry.get_gate(incident_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"No pending HITL gate for incident {incident_id!r}.",
            )
        try:
            await gate_registry.resolve(incident_id, body.action, body.comment)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"No pending HITL gate for incident {incident_id!r}.",
            )
        return ApprovalResponse(status="accepted", decision=body.action)

    return router
