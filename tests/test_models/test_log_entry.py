"""Tests for sentinel.models.log_entry."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel.models.log_entry import LogAnalysis, LogEntry, LogLevel, LogQuery

# ── LogLevel ──────────────────────────────────────────────────────────────────


def test_log_level_values() -> None:
    assert LogLevel.DEBUG == "DEBUG"
    assert LogLevel.INFO == "INFO"
    assert LogLevel.WARNING == "WARNING"
    assert LogLevel.ERROR == "ERROR"
    assert LogLevel.CRITICAL == "CRITICAL"


def test_log_level_from_string() -> None:
    assert LogLevel("ERROR") is LogLevel.ERROR


# ── LogEntry ──────────────────────────────────────────────────────────────────


def test_log_entry_required_fields() -> None:
    entry = LogEntry(level=LogLevel.ERROR, service="api-gateway", message="NPE in AuthMiddleware")
    assert entry.service == "api-gateway"
    assert entry.level is LogLevel.ERROR
    assert entry.message == "NPE in AuthMiddleware"
    assert entry.trace_id is None
    assert entry.timestamp.tzinfo is not None


def test_log_entry_with_trace_id() -> None:
    entry = LogEntry(
        level=LogLevel.INFO,
        service="user-service",
        message="request completed",
        trace_id="trace-abc123",
    )
    assert entry.trace_id == "trace-abc123"


def test_log_entry_explicit_timestamp() -> None:
    ts = datetime(2026, 5, 10, 3, 0, 1, tzinfo=UTC)
    entry = LogEntry(level=LogLevel.ERROR, service="svc", message="msg", timestamp=ts)
    assert entry.timestamp == ts


def test_log_entry_missing_message_raises() -> None:
    with pytest.raises(ValidationError):
        LogEntry(level=LogLevel.ERROR, service="svc")  # type: ignore[call-arg]


def test_log_entry_string_level_coerced() -> None:
    entry = LogEntry(level="WARNING", service="svc", message="msg")  # type: ignore[arg-type]
    assert entry.level is LogLevel.WARNING


def test_log_entry_json_round_trip() -> None:
    ts = datetime(2026, 5, 10, 3, 0, 0, tzinfo=UTC)
    entry = LogEntry(level=LogLevel.ERROR, service="api-gateway", message="boom", timestamp=ts)
    reloaded = LogEntry.model_validate_json(entry.model_dump_json())
    assert reloaded.service == entry.service
    assert reloaded.level is LogLevel.ERROR


# ── LogQuery ──────────────────────────────────────────────────────────────────


def test_log_query_required_fields() -> None:
    q = LogQuery(
        service="api-gateway",
        start_time=datetime(2026, 5, 10, 2, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 10, 3, 0, 0, tzinfo=UTC),
    )
    assert q.service == "api-gateway"
    assert q.level_filter is None
    assert q.keyword_filter is None


def test_log_query_with_filters() -> None:
    q = LogQuery(
        service="api-gateway",
        start_time=datetime(2026, 5, 10, 2, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 10, 3, 0, 0, tzinfo=UTC),
        level_filter=LogLevel.ERROR,
        keyword_filter="NullPointer",
    )
    assert q.level_filter is LogLevel.ERROR
    assert q.keyword_filter == "NullPointer"


# ── LogAnalysis ───────────────────────────────────────────────────────────────


def test_log_analysis_required_fields() -> None:
    analysis = LogAnalysis(
        anomaly_summary="Error rate spiked 7x at 03:00Z",
        hypothesis="NullPointerException in AuthMiddleware due to missing config key",
    )
    assert analysis.error_patterns == []
    assert analysis.key_log_lines == []
    assert "NullPointer" in analysis.hypothesis


def test_log_analysis_with_patterns_and_lines() -> None:
    entry = LogEntry(level=LogLevel.ERROR, service="api-gateway", message="NPE")
    analysis = LogAnalysis(
        error_patterns=["NullPointerException", "500 Internal Server Error"],
        anomaly_summary="Spike at 03:00Z",
        key_log_lines=[entry],
        hypothesis="Bad deploy introduced null ref",
    )
    assert len(analysis.error_patterns) == 2
    assert len(analysis.key_log_lines) == 1
    assert analysis.key_log_lines[0].level is LogLevel.ERROR


def test_log_analysis_json_round_trip() -> None:
    entry = LogEntry(level=LogLevel.ERROR, service="svc", message="err")
    analysis = LogAnalysis(
        anomaly_summary="spike",
        key_log_lines=[entry],
        hypothesis="bad config",
    )
    reloaded = LogAnalysis.model_validate_json(analysis.model_dump_json())
    assert reloaded.hypothesis == analysis.hypothesis
    assert len(reloaded.key_log_lines) == 1
