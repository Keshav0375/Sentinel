"""Tests for sentinel.infra.event_bus — EventBus pub/sub and PipelineEvent model."""

from __future__ import annotations

import asyncio

from sentinel.infra.event_bus import EventBus, EventType, PipelineEvent

# ── PipelineEvent model ──────────────────────────────────────────────────────


class TestPipelineEvent:
    def test_create_minimal(self) -> None:
        event = PipelineEvent(type=EventType.AGENT_STARTED, agent_name="triage_agent")
        assert event.type == EventType.AGENT_STARTED
        assert event.agent_name == "triage_agent"
        assert event.data == {}

    def test_create_with_data(self) -> None:
        event = PipelineEvent(
            type=EventType.TOOL_CALLED,
            agent_name="log_analyst_agent",
            incident_id="inc-001",
            data={"tool": "fetch_logs", "service": "api-gateway"},
        )
        assert event.data["tool"] == "fetch_logs"
        assert event.incident_id == "inc-001"

    def test_timestamp_auto_set(self) -> None:
        event = PipelineEvent(type=EventType.AGENT_COMPLETED, agent_name="triage_agent")
        assert event.timestamp is not None

    def test_serialization_roundtrip(self) -> None:
        event = PipelineEvent(
            type=EventType.HITL_REQUESTED,
            agent_name="remediation_agent",
            incident_id="inc-002",
            data={"action": "rollback", "risk": "high"},
        )
        json_str = event.model_dump_json()
        restored = PipelineEvent.model_validate_json(json_str)
        assert restored.type == event.type
        assert restored.agent_name == event.agent_name
        assert restored.data == event.data

    def test_all_event_types_exist(self) -> None:
        expected = {
            "agent_started",
            "agent_completed",
            "tool_called",
            "tool_result",
            "hitl_requested",
            "hitl_resolved",
            "incident_resolved",
        }
        actual = {e.value for e in EventType}
        assert actual == expected


# ── EventBus — publish/subscribe ─────────────────────────────────────────────


class TestEventBusSubscribe:
    async def test_subscribe_returns_queue(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        assert isinstance(q, asyncio.Queue)

    async def test_subscriber_count_increments(self) -> None:
        bus = EventBus()
        assert bus.subscriber_count == 0
        await bus.subscribe("inc-001")
        assert bus.subscriber_count == 1
        await bus.subscribe("inc-001")
        assert bus.subscriber_count == 2

    async def test_incident_subscriber_count(self) -> None:
        bus = EventBus()
        await bus.subscribe("inc-001")
        await bus.subscribe("inc-002")
        assert bus.incident_subscriber_count("inc-001") == 1
        assert bus.incident_subscriber_count("inc-002") == 1
        assert bus.incident_subscriber_count("inc-999") == 0


class TestEventBusPublish:
    async def test_publish_to_single_subscriber(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        event = PipelineEvent(type=EventType.AGENT_STARTED, agent_name="triage_agent")
        count = await bus.publish("inc-001", event)
        assert count == 1
        received = q.get_nowait()
        assert received is not None
        assert received.type == EventType.AGENT_STARTED

    async def test_publish_fans_out_to_multiple_subscribers(self) -> None:
        bus = EventBus()
        q1 = await bus.subscribe("inc-001")
        q2 = await bus.subscribe("inc-001")
        event = PipelineEvent(type=EventType.TOOL_CALLED, agent_name="log_analyst_agent")
        count = await bus.publish("inc-001", event)
        assert count == 2
        assert q1.get_nowait() == event
        assert q2.get_nowait() == event

    async def test_publish_to_wrong_incident_returns_zero(self) -> None:
        bus = EventBus()
        await bus.subscribe("inc-001")
        event = PipelineEvent(type=EventType.AGENT_STARTED, agent_name="triage_agent")
        count = await bus.publish("inc-002", event)
        assert count == 0

    async def test_publish_no_subscribers_returns_zero(self) -> None:
        bus = EventBus()
        event = PipelineEvent(type=EventType.AGENT_STARTED, agent_name="triage_agent")
        count = await bus.publish("inc-001", event)
        assert count == 0

    async def test_publish_does_not_cross_incidents(self) -> None:
        bus = EventBus()
        q1 = await bus.subscribe("inc-001")
        q2 = await bus.subscribe("inc-002")
        event = PipelineEvent(type=EventType.TOOL_RESULT, agent_name="triage_agent")
        await bus.publish("inc-001", event)
        assert q2.empty() is True
        assert q1.get_nowait() == event
        assert q2.empty()


class TestEventBusUnsubscribe:
    async def test_unsubscribe_removes_queue(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        assert bus.subscriber_count == 1
        await bus.unsubscribe("inc-001", q)
        assert bus.subscriber_count == 0

    async def test_unsubscribe_nonexistent_queue_does_not_raise(self) -> None:
        bus = EventBus()
        fake_q: asyncio.Queue[PipelineEvent | None] = asyncio.Queue()
        await bus.unsubscribe("inc-001", fake_q)

    async def test_unsubscribe_cleans_up_incident_key(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        await bus.unsubscribe("inc-001", q)
        assert bus.incident_subscriber_count("inc-001") == 0

    async def test_publish_after_unsubscribe_returns_zero(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        await bus.unsubscribe("inc-001", q)
        event = PipelineEvent(type=EventType.AGENT_STARTED, agent_name="triage_agent")
        count = await bus.publish("inc-001", event)
        assert count == 0


class TestEventBusCloseIncident:
    async def test_close_sends_none_sentinel(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        await bus.close_incident("inc-001")
        sentinel = q.get_nowait()
        assert sentinel is None

    async def test_close_removes_all_subscribers(self) -> None:
        bus = EventBus()
        await bus.subscribe("inc-001")
        await bus.subscribe("inc-001")
        assert bus.subscriber_count == 2
        await bus.close_incident("inc-001")
        assert bus.subscriber_count == 0

    async def test_close_nonexistent_incident_does_not_raise(self) -> None:
        bus = EventBus()
        await bus.close_incident("inc-does-not-exist")

    async def test_publish_after_close_returns_zero(self) -> None:
        bus = EventBus()
        await bus.subscribe("inc-001")
        await bus.close_incident("inc-001")
        event = PipelineEvent(type=EventType.AGENT_STARTED, agent_name="triage_agent")
        count = await bus.publish("inc-001", event)
        assert count == 0

    async def test_close_sends_sentinel_to_all_subscribers(self) -> None:
        bus = EventBus()
        q1 = await bus.subscribe("inc-001")
        q2 = await bus.subscribe("inc-001")
        await bus.close_incident("inc-001")
        assert q1.get_nowait() is None
        assert q2.get_nowait() is None


class TestEventBusMultipleIncidents:
    async def test_independent_incidents(self) -> None:
        bus = EventBus()
        q1 = await bus.subscribe("inc-001")
        q2 = await bus.subscribe("inc-002")
        e1 = PipelineEvent(type=EventType.AGENT_STARTED, agent_name="triage_agent")
        e2 = PipelineEvent(type=EventType.TOOL_CALLED, agent_name="log_analyst_agent")
        await bus.publish("inc-001", e1)
        await bus.publish("inc-002", e2)
        r1 = q1.get_nowait()
        r2 = q2.get_nowait()
        assert r1 is not None and r1.type == EventType.AGENT_STARTED
        assert r2 is not None and r2.type == EventType.TOOL_CALLED

    async def test_close_one_incident_leaves_other_intact(self) -> None:
        bus = EventBus()
        await bus.subscribe("inc-001")
        q2 = await bus.subscribe("inc-002")
        await bus.close_incident("inc-001")
        assert bus.incident_subscriber_count("inc-001") == 0
        assert bus.incident_subscriber_count("inc-002") == 1
        event = PipelineEvent(type=EventType.AGENT_COMPLETED, agent_name="comms_agent")
        count = await bus.publish("inc-002", event)
        assert count == 1
        assert q2.get_nowait() == event

    async def test_subscriber_count_across_incidents(self) -> None:
        bus = EventBus()
        await bus.subscribe("inc-001")
        await bus.subscribe("inc-001")
        await bus.subscribe("inc-002")
        assert bus.subscriber_count == 3


class TestEventBusOrdering:
    async def test_events_received_in_order(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        events = [
            PipelineEvent(
                type=EventType.AGENT_STARTED,
                agent_name="triage_agent",
                data={"seq": 0},
            ),
            PipelineEvent(
                type=EventType.TOOL_CALLED,
                agent_name="triage_agent",
                data={"seq": 1},
            ),
            PipelineEvent(
                type=EventType.TOOL_RESULT,
                agent_name="triage_agent",
                data={"seq": 2},
            ),
            PipelineEvent(
                type=EventType.AGENT_COMPLETED,
                agent_name="triage_agent",
                data={"seq": 3},
            ),
        ]
        for e in events:
            await bus.publish("inc-001", e)
        for i in range(4):
            received = q.get_nowait()
            assert received is not None
            assert received.data["seq"] == i

    async def test_queue_empty_after_consuming_all(self) -> None:
        bus = EventBus()
        q = await bus.subscribe("inc-001")
        event = PipelineEvent(type=EventType.AGENT_STARTED, agent_name="triage_agent")
        await bus.publish("inc-001", event)
        q.get_nowait()
        assert q.empty()
