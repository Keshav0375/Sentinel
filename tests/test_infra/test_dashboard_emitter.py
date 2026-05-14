"""Tests for DashboardEventEmitter — span → PipelineEvent publishing."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

from sentinel.infra.dashboard_emitter import DashboardEventEmitter, _incident_id
from sentinel.infra.event_bus import EventBus, EventType

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_span(
    span_data: Any,
    trace_id: str = "trace-001",
    span_id: str = "span-001",
    incident: str | None = "inc-001",
) -> MagicMock:
    span = MagicMock()
    span.span_data = span_data
    span.trace_id = trace_id
    span.span_id = span_id
    span.trace_metadata = {"incident_id": incident} if incident else {}
    return span


def _make_trace(trace_id: str = "trace-001") -> MagicMock:
    trace = MagicMock()
    trace.trace_id = trace_id
    return trace


def _agent_span_data(name: str = "triage_agent") -> MagicMock:
    from agents import AgentSpanData

    data = MagicMock(spec=AgentSpanData)
    data.name = name
    return data


def _func_span_data(
    name: str = "fetch_logs",
    input_val: str | None = "{}",
    output_val: str | None = "log entries",
) -> MagicMock:
    from agents import FunctionSpanData

    data = MagicMock(spec=FunctionSpanData)
    data.name = name
    data.input = input_val
    data.output = output_val
    return data


# ── _incident_id helper ───────────────────────────────────────────────────────


class TestIncidentId:
    def test_returns_id_from_metadata(self) -> None:
        span = _make_span(MagicMock(), incident="inc-abc")
        assert _incident_id(span) == "inc-abc"

    def test_returns_none_when_no_metadata(self) -> None:
        span = MagicMock()
        span.trace_metadata = {}
        assert _incident_id(span) is None

    def test_returns_none_when_attribute_missing(self) -> None:
        span = MagicMock(spec=[])  # no trace_metadata attribute
        assert _incident_id(span) is None


# ── DashboardEventEmitter structure ───────────────────────────────────────────


class TestDashboardEventEmitterStructure:
    def test_implements_tracing_processor(self) -> None:
        from agents import TracingProcessor

        bus = EventBus()
        emitter = DashboardEventEmitter(bus)
        assert isinstance(emitter, TracingProcessor)

    def test_shutdown_clears_active(self) -> None:
        bus = EventBus()
        emitter = DashboardEventEmitter(bus)
        emitter._active["trace-001"] = "triage_agent"
        emitter.shutdown()
        assert len(emitter._active) == 0

    def test_force_flush_does_not_raise(self) -> None:
        bus = EventBus()
        emitter = DashboardEventEmitter(bus)
        emitter.force_flush()  # should be a no-op

    def test_on_trace_start_does_not_raise(self) -> None:
        bus = EventBus()
        emitter = DashboardEventEmitter(bus)
        emitter.on_trace_start(_make_trace())

    def test_on_trace_end_clears_active_for_trace(self) -> None:
        bus = EventBus()
        emitter = DashboardEventEmitter(bus)
        emitter._active["trace-999"] = "triage_agent"
        trace = _make_trace("trace-999")
        emitter.on_trace_end(trace)
        assert "trace-999" not in emitter._active


# ── Agent span events ─────────────────────────────────────────────────────────


class TestAgentSpanEvents:
    async def test_agent_started_event_published(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        emitter = DashboardEventEmitter(bus)

        span = _make_span(_agent_span_data("triage_agent"))

        async def _emit_and_check() -> None:
            emitter.on_span_start(span)
            await asyncio.sleep(0)  # allow create_task to run

        await _emit_and_check()
        assert not q.empty()
        event = q.get_nowait()
        assert event is not None
        assert event.type == EventType.AGENT_STARTED
        assert event.agent_name == "triage_agent"

    async def test_agent_completed_event_published(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        emitter = DashboardEventEmitter(bus)
        emitter._active["trace-001"] = "triage_agent"

        span = _make_span(_agent_span_data("triage_agent"))

        async def _emit() -> None:
            emitter.on_span_end(span)
            await asyncio.sleep(0)

        await _emit()
        assert not q.empty()
        event = q.get_nowait()
        assert event is not None
        assert event.type == EventType.AGENT_COMPLETED

    async def test_agent_started_updates_active_map(self) -> None:
        bus = EventBus()
        emitter = DashboardEventEmitter(bus)
        span = _make_span(_agent_span_data("log_analyst_agent"))

        async def _emit() -> None:
            emitter.on_span_start(span)
            await asyncio.sleep(0)

        await _emit()
        assert emitter._active.get("trace-001") == "log_analyst_agent"

    async def test_no_event_published_without_incident_id(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        emitter = DashboardEventEmitter(bus)

        # Span with no incident metadata
        span = _make_span(_agent_span_data("triage_agent"), incident=None)

        async def _emit() -> None:
            emitter.on_span_start(span)
            await asyncio.sleep(0)

        await _emit()
        assert q.empty()


# ── Function (tool call) span events ──────────────────────────────────────────


class TestToolSpanEvents:
    async def test_tool_called_event_published(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        emitter = DashboardEventEmitter(bus)
        emitter._active["trace-001"] = "triage_agent"

        span = _make_span(_func_span_data("fetch_logs"))

        async def _emit() -> None:
            emitter.on_span_start(span)
            await asyncio.sleep(0)

        await _emit()
        event = q.get_nowait()
        assert event is not None
        assert event.type == EventType.TOOL_CALLED
        assert event.data["tool_name"] == "fetch_logs"

    async def test_tool_result_event_published(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        emitter = DashboardEventEmitter(bus)
        emitter._active["trace-001"] = "triage_agent"

        span = _make_span(_func_span_data("fetch_logs", output_val="10 log entries found"))

        async def _emit() -> None:
            emitter.on_span_end(span)
            await asyncio.sleep(0)

        await _emit()
        event = q.get_nowait()
        assert event is not None
        assert event.type == EventType.TOOL_RESULT
        assert "fetch_logs" in event.data.get("tool_name", "")

    async def test_tool_called_includes_call_id(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        emitter = DashboardEventEmitter(bus)
        emitter._active["trace-001"] = "triage_agent"

        span = _make_span(_func_span_data("get_service_metadata"), span_id="span-abc")

        async def _emit() -> None:
            emitter.on_span_start(span)
            await asyncio.sleep(0)

        await _emit()
        event = q.get_nowait()
        assert event is not None
        assert event.data.get("call_id") == "span-abc"

    async def test_tool_result_includes_output(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        emitter = DashboardEventEmitter(bus)

        span = _make_span(
            _func_span_data("get_service_metadata", output_val="api-gateway metadata"),
            span_id="span-xyz",
        )

        async def _emit() -> None:
            emitter.on_span_end(span)
            await asyncio.sleep(0)

        await _emit()
        event = q.get_nowait()
        assert event is not None
        assert "api-gateway" in event.data.get("output", "")

    async def test_tool_agent_name_from_active_map(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        emitter = DashboardEventEmitter(bus)
        emitter._active["trace-001"] = "log_analyst_agent"

        span = _make_span(_func_span_data("fetch_logs"))

        async def _emit() -> None:
            emitter.on_span_start(span)
            await asyncio.sleep(0)

        await _emit()
        event = q.get_nowait()
        assert event is not None
        assert event.agent_name == "log_analyst_agent"

    async def test_tool_called_none_input_handled(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        emitter = DashboardEventEmitter(bus)

        span = _make_span(_func_span_data("fetch_logs", input_val=None))

        async def _emit() -> None:
            emitter.on_span_start(span)
            await asyncio.sleep(0)

        await _emit()
        event = q.get_nowait()
        assert event is not None
        assert event.data.get("input") == ""


# ── _emit no-loop guard ───────────────────────────────────────────────────────


class TestEmitNoLoop:
    def test_emit_without_running_loop_does_not_raise(self) -> None:
        """_emit should silently skip publishing when no loop is running."""
        from sentinel.infra.event_bus import PipelineEvent

        bus = EventBus()
        emitter = DashboardEventEmitter(bus)
        event = PipelineEvent(
            type=EventType.AGENT_STARTED,
            agent_name="triage_agent",
            incident_id="inc-001",
        )
        # This is called synchronously outside any event loop
        emitter._emit("inc-001", event)  # must not raise
