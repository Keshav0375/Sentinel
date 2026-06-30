"""DashboardEventEmitter — publishes agent/tool span events to the EventBus.

Registered as a second ``TracingProcessor`` alongside ``SentinelTracer`` so
the live dashboard receives ``agent_started``, ``agent_completed``,
``tool_called``, and ``tool_result`` events in real time.

Usage::

    emitter = DashboardEventEmitter(event_bus)
    add_trace_processor(emitter)

Agent tracking heuristic:  the emitter maintains a ``trace_id → agent_name``
map updated on every ``AgentSpanData`` span start.  This lets tool-call events
carry the correct ``agent_name`` without requiring parent-span lookups.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agents import (
    AgentSpanData,
    FunctionSpanData,
    GenerationSpanData,
    Span,
    Trace,
    TracingProcessor,
)

from sentinel.infra.event_bus import EventBus, EventType, PipelineEvent
from sentinel.infra.logging import get_logger

logger = get_logger("sentinel.infra.dashboard_emitter")


class DashboardEventEmitter(TracingProcessor):
    """Converts Agents SDK spans into ``PipelineEvent`` objects for the dashboard.

    Only ``AgentSpanData`` and ``FunctionSpanData`` produce events.
    ``HandoffSpanData`` and ``GenerationSpanData`` are ignored — the former is
    captured by ``SentinelTracer`` and the latter is too granular for the UI.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        # trace_id → name of the most recently started agent in that trace
        self._active: dict[str, str] = {}
        # agent_name → last non-empty text output from a generation span
        self._last_output: dict[str, str] = {}

    # ── TracingProcessor interface ────────────────────────────────────────────

    def on_trace_start(self, trace: Trace) -> None:
        pass

    def on_trace_end(self, trace: Trace) -> None:
        self._active.pop(trace.trace_id, None)

    def on_span_start(self, span: Span[Any]) -> None:
        incident_id = _incident_id(span)
        if not incident_id:
            return
        data = span.span_data

        if isinstance(data, AgentSpanData):
            agent = data.name or "agent"
            self._active[span.trace_id] = agent
            self._emit(
                incident_id,
                PipelineEvent(
                    type=EventType.AGENT_STARTED,
                    agent_name=agent,
                    incident_id=incident_id,
                ),
            )

        elif isinstance(data, FunctionSpanData):
            agent = self._active.get(span.trace_id, "agent")
            self._emit(
                incident_id,
                PipelineEvent(
                    type=EventType.TOOL_CALLED,
                    agent_name=agent,
                    incident_id=incident_id,
                    data={
                        "tool_name": data.name,
                        "call_id": span.span_id,
                        "input": str(data.input)[:200] if data.input is not None else "",
                    },
                ),
            )

    def on_span_end(self, span: Span[Any]) -> None:
        incident_id = _incident_id(span)
        if not incident_id:
            return
        data = span.span_data

        if isinstance(data, GenerationSpanData):
            agent = self._active.get(span.trace_id, "agent")
            if data.output:
                for msg in data.output:
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if content and isinstance(content, str) and len(content) > 20:
                        self._last_output[agent] = content[:500]

        elif isinstance(data, AgentSpanData):
            agent = self._active.get(span.trace_id, data.name or "agent")
            output = self._last_output.pop(agent, None)
            event_data: dict[str, object] = {"output": output} if output else {}
            self._emit(
                incident_id,
                PipelineEvent(
                    type=EventType.AGENT_COMPLETED,
                    agent_name=agent,
                    incident_id=incident_id,
                    data=event_data,
                ),
            )

        elif isinstance(data, FunctionSpanData):
            agent = self._active.get(span.trace_id, "agent")
            self._emit(
                incident_id,
                PipelineEvent(
                    type=EventType.TOOL_RESULT,
                    agent_name=agent,
                    incident_id=incident_id,
                    data={
                        "tool_name": data.name,
                        "call_id": span.span_id,
                        "output": str(data.output)[:300] if data.output is not None else "",
                    },
                ),
            )

    def shutdown(self) -> None:
        self._active.clear()
        self._last_output.clear()

    def force_flush(self) -> None:
        pass

    # ── Internal ──────────────────────────────────────────────────────────────

    def _emit(self, incident_id: str, event: PipelineEvent) -> None:
        """Schedule an async publish on the running event loop.

        Called synchronously from span callbacks — schedules the coroutine
        on the event loop that is already running the agent pipeline so
        no cross-loop issues arise.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._bus.publish(incident_id, event))
        except RuntimeError:
            # No running loop (e.g. called from a test without an event loop).
            logger.debug("dashboard_emitter_no_loop", event_type=str(event.type))


def _incident_id(span: Span[Any]) -> str | None:
    """Extract incident_id from span trace metadata, or return None."""
    metadata = getattr(span, "trace_metadata", None)
    if isinstance(metadata, dict):
        typed: dict[str, object] = metadata  # type: ignore[assignment]
        value = typed.get("incident_id")
        return str(value) if value is not None else None
    return None
