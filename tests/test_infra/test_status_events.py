"""Tests for sentinel.infra.status_events — the ``sentinel:events`` publisher."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from redis_stub import StubRedis, stub_handle

from sentinel.infra import status_events as events_module
from sentinel.infra.status_events import STATUS_CHANNEL, StatusPublisher

AT = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


async def test_publish_shape_on_channel() -> None:
    stub = StubRedis()
    publisher = StatusPublisher(stub_handle(stub), clock=lambda: AT)

    assert publisher.publish("inc-1", "in_progress") is None  # type: ignore[func-returns-value]
    publisher.publish("inc-1", "escalated")
    await publisher.drain(2.0)

    assert [channel for channel, _ in stub.published] == [STATUS_CHANNEL] * 2
    assert STATUS_CHANNEL == "sentinel:events"
    first, second = (json.loads(message) for _, message in stub.published)
    assert first == {
        "v": 1,
        "incident_id": "inc-1",
        "status": "in_progress",
        "at": "2026-10-28T12:00:00+00:00",
    }
    assert second["status"] == "escalated"


async def test_publish_noop_without_redis() -> None:
    publisher = StatusPublisher(None)
    publisher.publish("inc-1", "in_progress")
    await publisher.drain(0.1)


async def test_publish_failure_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(events_module, "logger", log)
    stub = StubRedis()
    stub.fail = True
    publisher = StatusPublisher(stub_handle(stub))

    publisher.publish("inc-1", "resumed")
    await publisher.drain(2.0)

    log.warning.assert_called_once_with(
        "status_event_failed",
        incident_id="inc-1",
        status="resumed",
        error_type="ConnectionError",
    )
