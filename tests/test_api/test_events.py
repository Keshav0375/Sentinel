"""Tests for sentinel.api.events — the SSE endpoint, and that no approval route exists."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from sentinel.api.events import make_events_router
from sentinel.infra.event_bus import EventBus, EventType, PipelineEvent

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_test_app(event_bus: EventBus | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(make_events_router(event_bus=event_bus or EventBus()))
    return app


# ── No approval path (backend.md §3.3: the revert PR is the only gate) ────────


def test_router_serves_only_the_sse_stream() -> None:
    router = make_events_router(event_bus=EventBus())
    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    assert len(routes) == len(router.routes)
    assert [(r.path, r.methods) for r in routes] == [("/events/{incident_id}", {"GET"})]


def test_approve_route_is_gone() -> None:
    with TestClient(_make_test_app()) as client:
        resp = client.post("/incidents/inc-001/approve", json={"action": "approve"})
    assert resp.status_code == 404


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
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
