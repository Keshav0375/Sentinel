"""Tests for sentinel.infra.tracing — SentinelTracer and helper functions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from sentinel.infra.tracing import (
    SentinelTracer,
    _extract_incident_id,
    _incident_id_from_span,
    _parse_ended_at,
    _save_trajectory,
    _span_to_timeline_entry,
)
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource

# ── Mock helpers ──────────────────────────────────────────────────────────────

def _mock_trace(incident_id: str | None = "inc-test0001", name: str = "test") -> Any:
    """Create a minimal Trace mock with metadata."""
    t = MagicMock()
    t.trace_id = "trace_abc123"
    t.name = name
    t.metadata = {"incident_id": incident_id} if incident_id is not None else {}
    t.export.return_value = {"workflow_name": name, "id": "trace_abc123"}
    return t


def _mock_span(
    trace_id: str = "trace_abc123",
    incident_id: str | None = "inc-test0001",
    span_data: Any = None,
    ended_at: str | None = None,
) -> Any:
    """Create a minimal Span mock with trace_metadata."""
    s = MagicMock()
    s.trace_id = trace_id
    s.span_id = "span_xyz456"
    s.trace_metadata = {"incident_id": incident_id} if incident_id else {}
    s.span_data = span_data or MagicMock()
    s.ended_at = ended_at or datetime.now(UTC).isoformat()
    s.export.return_value = {"id": "span_xyz456", "type": "test"}
    return s


def _function_span_data(name: str = "fetch_logs", output: str = "10 log entries") -> Any:
    """Create a FunctionSpanData mock."""
    from agents import FunctionSpanData
    return FunctionSpanData(name=name, input="query", output=output)


def _handoff_span_data(from_agent: str = "orchestrator", to_agent: str = "triage_agent") -> Any:
    """Create a HandoffSpanData mock."""
    from agents import HandoffSpanData
    return HandoffSpanData(from_agent=from_agent, to_agent=to_agent)


def _make_stm(incident_id: str = "inc-test0001") -> ShortTermMemory:
    stm = ShortTermMemory()
    alert = AlertPayload(
        source=AlertSource.DATADOG,
        service="api-gateway",
        metric="error_rate",
        threshold=0.05,
        current_value=0.34,
        severity=AlertSeverity.CRITICAL,
    )
    stm.create(incident_id, alert)
    return stm


# ── _extract_incident_id ──────────────────────────────────────────────────────

def test_extract_incident_id_found() -> None:
    trace = _mock_trace("inc-abc12345")
    assert _extract_incident_id(trace) == "inc-abc12345"


def test_extract_incident_id_missing_key() -> None:
    trace = _mock_trace()
    trace.metadata = {"other": "value"}
    assert _extract_incident_id(trace) is None


def test_extract_incident_id_no_metadata() -> None:
    trace = _mock_trace()
    trace.metadata = None
    assert _extract_incident_id(trace) is None


def test_extract_incident_id_not_dict() -> None:
    trace = _mock_trace()
    trace.metadata = "not-a-dict"
    assert _extract_incident_id(trace) is None


def test_extract_incident_id_converts_to_str() -> None:
    trace = _mock_trace()
    trace.metadata = {"incident_id": 42}  # non-string value
    result = _extract_incident_id(trace)
    assert result == "42"


# ── _incident_id_from_span ────────────────────────────────────────────────────

def test_incident_id_from_span_found() -> None:
    span = _mock_span(incident_id="inc-span0001")
    assert _incident_id_from_span(span) == "inc-span0001"


def test_incident_id_from_span_none_metadata() -> None:
    span = _mock_span()
    span.trace_metadata = None
    assert _incident_id_from_span(span) is None


def test_incident_id_from_span_empty_metadata() -> None:
    span = _mock_span(incident_id=None)
    assert _incident_id_from_span(span) is None


# ── _span_to_timeline_entry ───────────────────────────────────────────────────

def test_span_to_entry_function_span() -> None:
    data = _function_span_data("fetch_logs", "15 entries found")
    span = _mock_span(span_data=data)
    entry = _span_to_timeline_entry(span)
    assert entry is not None
    assert entry.action == "fetch_logs"
    assert "15 entries found" in entry.result_summary


def test_span_to_entry_function_span_no_output() -> None:
    data = _function_span_data("fetch_logs", None)
    data.output = None
    span = _mock_span(span_data=data)
    entry = _span_to_timeline_entry(span)
    assert entry is not None
    assert entry.result_summary == "no output"


def test_span_to_entry_function_span_truncates_long_output() -> None:
    long_output = "x" * 400
    data = _function_span_data("big_tool", long_output)
    span = _mock_span(span_data=data)
    entry = _span_to_timeline_entry(span)
    assert entry is not None
    assert len(entry.result_summary) <= 300


def test_span_to_entry_handoff_span() -> None:
    data = _handoff_span_data("orchestrator", "triage_agent")
    span = _mock_span(span_data=data)
    entry = _span_to_timeline_entry(span)
    assert entry is not None
    assert entry.agent_name == "orchestrator"
    assert "triage_agent" in entry.action
    assert "triage_agent" in entry.result_summary


def test_span_to_entry_handoff_none_from_agent() -> None:
    from agents import HandoffSpanData
    data = HandoffSpanData(from_agent=None, to_agent="comms_agent")
    span = _mock_span(span_data=data)
    entry = _span_to_timeline_entry(span)
    assert entry is not None
    assert entry.agent_name == "orchestrator"  # defaults to "orchestrator"


def test_span_to_entry_other_span_type_returns_none() -> None:
    from agents import AgentSpanData, GenerationSpanData
    for data in [AgentSpanData("triage_agent"), GenerationSpanData()]:
        span = _mock_span(span_data=data)
        assert _span_to_timeline_entry(span) is None


# ── _parse_ended_at ───────────────────────────────────────────────────────────

def test_parse_ended_at_valid_iso() -> None:
    span = _mock_span(ended_at="2026-05-12T15:30:00Z")
    result = _parse_ended_at(span)
    assert result.year == 2026
    assert result.month == 5


def test_parse_ended_at_none_defaults_to_now() -> None:
    span = _mock_span()
    span.ended_at = None
    before = datetime.now(UTC)
    result = _parse_ended_at(span)
    assert result >= before


def test_parse_ended_at_invalid_string_defaults_to_now() -> None:
    span = _mock_span()
    span.ended_at = "not-a-date"
    before = datetime.now(UTC)
    result = _parse_ended_at(span)
    assert result >= before


# ── _save_trajectory ──────────────────────────────────────────────────────────

def test_save_trajectory_creates_file(tmp_path: Path) -> None:
    _save_trajectory(
        incident_id="inc-savetest",
        trace_export={"workflow_name": "test"},
        spans=[{"type": "function", "name": "fetch_logs"}],
        trajectories_dir=tmp_path,
    )
    output = tmp_path / "inc-savetest.json"
    assert output.exists()


def test_save_trajectory_valid_json(tmp_path: Path) -> None:
    _save_trajectory(
        incident_id="inc-jsontest",
        trace_export={"id": "trace_123"},
        spans=[{"type": "handoff"}],
        trajectories_dir=tmp_path,
    )
    data = json.loads((tmp_path / "inc-jsontest.json").read_text())
    assert data["incident_id"] == "inc-jsontest"
    assert data["trace"]["id"] == "trace_123"
    assert len(data["spans"]) == 1
    assert "saved_at" in data


def test_save_trajectory_creates_parent_dirs(tmp_path: Path) -> None:
    deep_dir = tmp_path / "a" / "b" / "c"
    _save_trajectory(
        incident_id="inc-deeptest",
        trace_export={},
        spans=[],
        trajectories_dir=deep_dir,
    )
    assert (deep_dir / "inc-deeptest.json").exists()


# ── SentinelTracer ────────────────────────────────────────────────────────────

def test_tracer_on_span_end_tool_call_adds_timeline_entry() -> None:
    stm = _make_stm()
    tracer = SentinelTracer(stm)
    # Prime the internal spans dict so on_span_end knows about this trace
    tracer._spans["trace_abc123"] = []

    data = _function_span_data("get_service_metadata", "api-gateway metadata")
    span = _mock_span(span_data=data)
    tracer.on_span_end(span)

    timeline = stm.get_timeline("inc-test0001")
    assert len(timeline) == 1
    assert timeline[0].action == "get_service_metadata"


def test_tracer_on_span_end_handoff_adds_timeline_entry() -> None:
    stm = _make_stm()
    tracer = SentinelTracer(stm)
    tracer._spans["trace_abc123"] = []

    data = _handoff_span_data("orchestrator", "log_analyst_agent")
    span = _mock_span(span_data=data)
    tracer.on_span_end(span)

    timeline = stm.get_timeline("inc-test0001")
    assert len(timeline) == 1
    assert "log_analyst_agent" in timeline[0].action


def test_tracer_on_span_end_no_incident_id_skips() -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm)

    span = _mock_span(incident_id=None)
    tracer.on_span_end(span)  # must not raise

    assert stm.active_count == 0


def test_tracer_on_span_end_cleared_incident_does_not_raise() -> None:
    """If the incident is cleared between span start and end, no KeyError."""
    stm = _make_stm()
    tracer = SentinelTracer(stm)
    tracer._spans["trace_abc123"] = []
    stm.clear("inc-test0001")  # clear before span ends

    data = _function_span_data()
    span = _mock_span(span_data=data)
    tracer.on_span_end(span)  # must not raise


def test_tracer_on_trace_start_primes_spans_dict() -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm)
    trace = _mock_trace("inc-primetest")
    tracer.on_trace_start(trace)
    assert "trace_abc123" in tracer._spans


def test_tracer_on_trace_start_ignores_non_sentinel_traces() -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm)
    trace = _mock_trace(incident_id=None)
    trace.metadata = {}  # no incident_id
    tracer.on_trace_start(trace)
    assert "trace_abc123" not in tracer._spans


def test_tracer_on_trace_end_writes_trajectory(tmp_path: Path) -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm, trajectories_dir=tmp_path)
    trace = _mock_trace("inc-trajtest")
    tracer.on_trace_start(trace)

    # Add a fake span to the internal buffer
    tracer._spans["trace_abc123"].append({"type": "function", "name": "fetch_logs"})

    tracer.on_trace_end(trace)
    assert (tmp_path / "inc-trajtest.json").exists()


def test_tracer_on_trace_end_no_dir_does_not_write(tmp_path: Path) -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm, trajectories_dir=None)
    trace = _mock_trace("inc-nowrite")
    tracer.on_trace_start(trace)
    tracer.on_trace_end(trace)
    # No files should be written anywhere
    assert list(tmp_path.iterdir()) == []


def test_tracer_on_trace_end_cleans_spans_dict() -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm)
    trace = _mock_trace("inc-cleanup")
    tracer.on_trace_start(trace)
    assert "trace_abc123" in tracer._spans
    tracer.on_trace_end(trace)
    assert "trace_abc123" not in tracer._spans


def test_tracer_shutdown_clears_state() -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm)
    tracer._spans["trace_123"] = [{"type": "span"}]
    tracer.shutdown()
    assert len(tracer._spans) == 0


def test_tracer_multiple_incidents_isolated(tmp_path: Path) -> None:
    """Events from two concurrent traces must not cross-contaminate."""
    stm = ShortTermMemory()
    alert = AlertPayload(
        source=AlertSource.DATADOG, service="svc", metric="m",
        threshold=0.1, current_value=0.9, severity=AlertSeverity.CRITICAL,
    )
    stm.create("inc-alpha001", alert)
    stm.create("inc-beta0001", alert)

    tracer = SentinelTracer(stm, trajectories_dir=tmp_path)

    # Simulate two separate traces
    trace_a = MagicMock()
    trace_a.trace_id = "trace_aaa"
    trace_a.metadata = {"incident_id": "inc-alpha001"}
    trace_a.export.return_value = {}

    trace_b = MagicMock()
    trace_b.trace_id = "trace_bbb"
    trace_b.metadata = {"incident_id": "inc-beta0001"}
    trace_b.export.return_value = {}

    tracer.on_trace_start(trace_a)
    tracer.on_trace_start(trace_b)

    # Tool call on trace A
    data_a = _function_span_data("fetch_logs", "logs for alpha")
    span_a = MagicMock()
    span_a.trace_id = "trace_aaa"
    span_a.span_data = data_a
    span_a.trace_metadata = {"incident_id": "inc-alpha001"}
    span_a.ended_at = None
    span_a.export.return_value = {"type": "function"}

    tracer.on_span_end(span_a)

    # Tool call on trace B
    data_b = _function_span_data("list_recent_deploys", "deploys for beta")
    span_b = MagicMock()
    span_b.trace_id = "trace_bbb"
    span_b.span_data = data_b
    span_b.trace_metadata = {"incident_id": "inc-beta0001"}
    span_b.ended_at = None
    span_b.export.return_value = {"type": "function"}

    tracer.on_span_end(span_b)

    timeline_a = stm.get_timeline("inc-alpha001")
    timeline_b = stm.get_timeline("inc-beta0001")

    assert len(timeline_a) == 1
    assert timeline_a[0].action == "fetch_logs"
    assert len(timeline_b) == 1
    assert timeline_b[0].action == "list_recent_deploys"
