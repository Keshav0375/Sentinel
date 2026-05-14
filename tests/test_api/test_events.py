"""Tests for sentinel.api.events — HitlGateRegistry, approval_fn, and HTTP endpoints."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinel.api.events import (
    ApprovalDecision,
    HitlGate,
    HitlGateRegistry,
    make_api_approval_fn,
    make_events_router,
    reset_current_incident,
    set_current_incident,
)
from sentinel.infra.event_bus import EventBus, EventType, PipelineEvent

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_test_app(
    event_bus: EventBus | None = None,
    gate_registry: HitlGateRegistry | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        make_events_router(
            event_bus=event_bus or EventBus(),
            gate_registry=gate_registry or HitlGateRegistry(),
        )
    )
    return app


# ── HitlGate ──────────────────────────────────────────────────────────────────


class TestHitlGate:
    def test_gate_starts_unset(self) -> None:
        gate = HitlGate()
        assert not gate.event.is_set()

    def test_gate_default_decision_is_reject(self) -> None:
        gate = HitlGate()
        assert gate.decision == "reject"

    def test_gate_default_comment_is_none(self) -> None:
        gate = HitlGate()
        assert gate.comment is None

    async def test_gate_event_can_be_set(self) -> None:
        gate = HitlGate()
        gate.event.set()
        assert gate.event.is_set()


# ── HitlGateRegistry ──────────────────────────────────────────────────────────


class TestHitlGateRegistry:
    def test_create_gate_returns_hitl_gate(self) -> None:
        registry = HitlGateRegistry()
        gate = registry.create_gate("inc-001")
        assert isinstance(gate, HitlGate)

    def test_create_gate_stores_it(self) -> None:
        registry = HitlGateRegistry()
        registry.create_gate("inc-001")
        assert registry.get_gate("inc-001") is not None

    def test_get_gate_missing_returns_none(self) -> None:
        registry = HitlGateRegistry()
        assert registry.get_gate("inc-does-not-exist") is None

    def test_create_gate_replaces_existing(self) -> None:
        registry = HitlGateRegistry()
        g1 = registry.create_gate("inc-001")
        g2 = registry.create_gate("inc-001")
        assert g1 is not g2
        assert registry.get_gate("inc-001") is g2

    def test_clear_removes_gate(self) -> None:
        registry = HitlGateRegistry()
        registry.create_gate("inc-001")
        registry.clear("inc-001")
        assert registry.get_gate("inc-001") is None

    def test_clear_nonexistent_does_not_raise(self) -> None:
        registry = HitlGateRegistry()
        registry.clear("inc-does-not-exist")

    def test_active_count_tracks_gates(self) -> None:
        registry = HitlGateRegistry()
        assert registry.active_count == 0
        registry.create_gate("inc-001")
        assert registry.active_count == 1
        registry.create_gate("inc-002")
        assert registry.active_count == 2
        registry.clear("inc-001")
        assert registry.active_count == 1

    async def test_resolve_sets_event(self) -> None:
        registry = HitlGateRegistry()
        gate = registry.create_gate("inc-001")
        await registry.resolve("inc-001", "approve")
        assert gate.event.is_set()

    async def test_resolve_stores_decision(self) -> None:
        registry = HitlGateRegistry()
        gate = registry.create_gate("inc-001")
        await registry.resolve("inc-001", "approve", comment="looks good")
        assert gate.decision == "approve"
        assert gate.comment == "looks good"

    async def test_resolve_reject_stores_decision(self) -> None:
        registry = HitlGateRegistry()
        gate = registry.create_gate("inc-001")
        await registry.resolve("inc-001", "reject", comment="too risky")
        assert gate.decision == "reject"
        assert gate.comment == "too risky"

    async def test_resolve_missing_gate_raises_key_error(self) -> None:
        registry = HitlGateRegistry()
        with pytest.raises(KeyError):
            await registry.resolve("inc-does-not-exist", "approve")


# ── set_current_incident / reset_current_incident ────────────────────────────


class TestIncidentContext:
    async def test_set_and_get_incident_id(self) -> None:
        from sentinel.api.events import _CURRENT_INCIDENT_ID

        token = set_current_incident("inc-001")
        try:
            assert _CURRENT_INCIDENT_ID.get() == "inc-001"
        finally:
            reset_current_incident(token)

    async def test_reset_restores_default(self) -> None:
        from sentinel.api.events import _CURRENT_INCIDENT_ID

        token = set_current_incident("inc-001")
        reset_current_incident(token)
        assert _CURRENT_INCIDENT_ID.get() == ""

    async def test_nested_contexts(self) -> None:
        from sentinel.api.events import _CURRENT_INCIDENT_ID

        token1 = set_current_incident("inc-001")
        assert _CURRENT_INCIDENT_ID.get() == "inc-001"

        token2 = set_current_incident("inc-002")
        assert _CURRENT_INCIDENT_ID.get() == "inc-002"

        reset_current_incident(token2)
        assert _CURRENT_INCIDENT_ID.get() == "inc-001"

        reset_current_incident(token1)
        assert _CURRENT_INCIDENT_ID.get() == ""


# ── make_api_approval_fn ──────────────────────────────────────────────────────


class TestApiApprovalFn:
    async def test_approval_fn_returns_approve_when_resolved(self) -> None:
        bus = EventBus()
        registry = HitlGateRegistry()
        approval_fn = make_api_approval_fn(bus, registry)

        token = set_current_incident("inc-001")
        try:
            async def _resolve_after_delay() -> None:
                await asyncio.sleep(0)  # yield to let approval_fn start
                await registry.resolve("inc-001", "approve", "all good")

            task = asyncio.create_task(_resolve_after_delay())
            decision, comment = await approval_fn("HITL request display")
            await task
        finally:
            reset_current_incident(token)

        assert decision == "approve"
        assert comment == "all good"

    async def test_approval_fn_returns_reject_when_resolved(self) -> None:
        bus = EventBus()
        registry = HitlGateRegistry()
        approval_fn = make_api_approval_fn(bus, registry)

        token = set_current_incident("inc-001")
        try:
            async def _resolve_reject() -> None:
                await asyncio.sleep(0)
                await registry.resolve("inc-001", "reject", "too dangerous")

            task = asyncio.create_task(_resolve_reject())
            decision, comment = await approval_fn("HITL request display")
            await task
        finally:
            reset_current_incident(token)

        assert decision == "reject"
        assert comment == "too dangerous"

    async def test_approval_fn_publishes_hitl_requested_event(self) -> None:
        bus = EventBus()
        registry = HitlGateRegistry()
        approval_fn = make_api_approval_fn(bus, registry)
        q = await bus.subscribe("inc-001")

        token = set_current_incident("inc-001")
        try:
            async def _resolve() -> None:
                await asyncio.sleep(0)
                await registry.resolve("inc-001", "approve")

            task = asyncio.create_task(_resolve())
            await approval_fn("display text")
            await task
        finally:
            reset_current_incident(token)

        events = []
        while not q.empty():
            e = q.get_nowait()
            if e is not None:
                events.append(e)

        types = [e.type for e in events]
        assert EventType.HITL_REQUESTED in types

    async def test_approval_fn_publishes_hitl_resolved_event(self) -> None:
        bus = EventBus()
        registry = HitlGateRegistry()
        approval_fn = make_api_approval_fn(bus, registry)
        q = await bus.subscribe("inc-001")

        token = set_current_incident("inc-001")
        try:
            async def _resolve() -> None:
                await asyncio.sleep(0)
                await registry.resolve("inc-001", "approve")

            task = asyncio.create_task(_resolve())
            await approval_fn("display text")
            await task
        finally:
            reset_current_incident(token)

        events = []
        while not q.empty():
            e = q.get_nowait()
            if e is not None:
                events.append(e)

        types = [e.type for e in events]
        assert EventType.HITL_RESOLVED in types

    async def test_approval_fn_without_incident_context_returns_reject(self) -> None:
        bus = EventBus()
        registry = HitlGateRegistry()
        approval_fn = make_api_approval_fn(bus, registry)

        # No incident context set — should immediately return reject
        decision, comment = await approval_fn("display text")
        assert decision == "reject"
        assert comment is not None

    async def test_approval_fn_clears_gate_after_resolution(self) -> None:
        bus = EventBus()
        registry = HitlGateRegistry()
        approval_fn = make_api_approval_fn(bus, registry)

        token = set_current_incident("inc-001")
        try:
            async def _resolve() -> None:
                await asyncio.sleep(0)
                await registry.resolve("inc-001", "approve")

            task = asyncio.create_task(_resolve())
            await approval_fn("display text")
            await task
        finally:
            reset_current_incident(token)

        assert registry.get_gate("inc-001") is None


# ── POST /incidents/{incident_id}/approve ─────────────────────────────────────


class TestApproveEndpoint:
    def test_approve_returns_404_when_no_gate(self) -> None:
        app = _make_test_app()
        with TestClient(app) as client:
            resp = client.post(
                "/incidents/inc-001/approve",
                json={"action": "approve"},
            )
        assert resp.status_code == 404

    def test_approve_returns_202_when_gate_exists(self) -> None:
        registry = HitlGateRegistry()
        registry.create_gate("inc-001")
        app = _make_test_app(gate_registry=registry)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/incidents/inc-001/approve",
                json={"action": "approve"},
            )
        assert resp.status_code == 202

    def test_approve_response_contains_decision(self) -> None:
        registry = HitlGateRegistry()
        registry.create_gate("inc-001")
        app = _make_test_app(gate_registry=registry)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/incidents/inc-001/approve",
                json={"action": "approve", "comment": "ship it"},
            )
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["decision"] == "approve"

    def test_reject_stores_decision(self) -> None:
        registry = HitlGateRegistry()
        registry.create_gate("inc-001")
        app = _make_test_app(gate_registry=registry)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/incidents/inc-001/approve",
                json={"action": "reject", "comment": "too risky"},
            )
        assert resp.status_code == 202
        assert resp.json()["decision"] == "reject"

    def test_invalid_action_returns_422(self) -> None:
        registry = HitlGateRegistry()
        registry.create_gate("inc-001")
        app = _make_test_app(gate_registry=registry)
        with TestClient(app) as client:
            resp = client.post(
                "/incidents/inc-001/approve",
                json={"action": "maybe"},
            )
        assert resp.status_code == 422


# ── GET /events/{incident_id} (SSE endpoint) ──────────────────────────────────


class TestSseEndpoint:
    async def test_sse_returns_event_stream_content_type(self) -> None:
        """SSE endpoint sets text/event-stream Content-Type (async ASGI client)."""
        import httpx

        bus = EventBus()
        app = _make_test_app(event_bus=bus)
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]

        content_type: str = ""

        async def _stream_and_check() -> None:
            nonlocal content_type
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                async with client.stream("GET", "/events/inc-sse-ct") as resp:
                    content_type = resp.headers.get("content-type", "")
                    async for _ in resp.aiter_text():
                        pass

        async def _close() -> None:
            await asyncio.sleep(0.05)
            await bus.close_incident("inc-sse-ct")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_stream_and_check())
            tg.create_task(_close())

        assert "text/event-stream" in content_type

    async def test_sse_route_exists(self) -> None:
        """GET /events/{incident_id} returns HTTP 200 (async ASGI client)."""
        import httpx

        bus = EventBus()
        app = _make_test_app(event_bus=bus)
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]

        status_code: int = 0

        async def _check_status() -> None:
            nonlocal status_code
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                async with client.stream("GET", "/events/inc-sse-re") as resp:
                    status_code = resp.status_code
                    async for _ in resp.aiter_text():
                        pass

        async def _close() -> None:
            await asyncio.sleep(0.05)
            await bus.close_incident("inc-sse-re")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_check_status())
            tg.create_task(_close())

        assert status_code == 200

    async def test_sse_streams_event_then_closes_on_sentinel(self) -> None:
        """SSE endpoint streams events and terminates cleanly on None sentinel."""
        import httpx

        bus = EventBus()
        app = _make_test_app(event_bus=bus)
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        event = PipelineEvent(
            type=EventType.AGENT_STARTED,
            agent_name="triage_agent",
            incident_id="inc-sse-stream",
        )

        received_text = ""

        async def _stream_events() -> None:
            nonlocal received_text
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                async with client.stream("GET", "/events/inc-sse-stream") as resp:
                    async for chunk in resp.aiter_text():
                        received_text += chunk

        async def _send_and_close() -> None:
            await asyncio.sleep(0.05)
            await bus.publish("inc-sse-stream", event)
            await bus.close_incident("inc-sse-stream")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_stream_events())
            tg.create_task(_send_and_close())

        assert "agent_started" in received_text

    def test_approve_model_valid_actions(self) -> None:
        body = ApprovalDecision(action="approve")
        assert body.action == "approve"
        body2 = ApprovalDecision(action="reject", comment="no")
        assert body2.comment == "no"
