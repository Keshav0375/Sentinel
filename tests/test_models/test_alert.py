"""Tests for sentinel.models.alert."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel.models.alert import AlertAck, AlertPayload, AlertSeverity, AlertSource

# ── AlertSource ───────────────────────────────────────────────────────────────


def test_alert_source_values() -> None:
    assert AlertSource.DATADOG == "datadog"
    assert AlertSource.PAGERDUTY == "pagerduty"


def test_alert_source_from_string() -> None:
    assert AlertSource("datadog") is AlertSource.DATADOG
    assert AlertSource("pagerduty") is AlertSource.PAGERDUTY


# ── AlertSeverity ─────────────────────────────────────────────────────────────


def test_alert_severity_values() -> None:
    assert AlertSeverity.CRITICAL == "critical"
    assert AlertSeverity.HIGH == "high"
    assert AlertSeverity.WARNING == "warning"
    assert AlertSeverity.LOW == "low"
    assert AlertSeverity.INFO == "info"


# ── AlertPayload ──────────────────────────────────────────────────────────────


def _minimal_payload(**overrides: object) -> AlertPayload:
    defaults: dict[str, object] = {
        "source": AlertSource.DATADOG,
        "service": "api-gateway",
        "metric": "error_rate",
        "threshold": 0.05,
        "current_value": 0.34,
        "severity": AlertSeverity.CRITICAL,
    }
    defaults.update(overrides)
    return AlertPayload(**defaults)  # type: ignore[arg-type]


def test_alert_payload_required_fields() -> None:
    p = _minimal_payload()
    assert p.source is AlertSource.DATADOG
    assert p.service == "api-gateway"
    assert p.metric == "error_rate"
    assert p.threshold == 0.05
    assert p.current_value == 0.34
    assert p.severity is AlertSeverity.CRITICAL


def test_alert_payload_defaults_populated() -> None:
    p = _minimal_payload()
    assert p.alert_id != ""
    assert p.timestamp.tzinfo is not None  # timezone-aware
    assert p.metadata == {}


def test_alert_payload_alert_id_unique() -> None:
    p1 = _minimal_payload()
    p2 = _minimal_payload()
    assert p1.alert_id != p2.alert_id


def test_alert_payload_explicit_alert_id() -> None:
    p = _minimal_payload(alert_id="alert-123")
    assert p.alert_id == "alert-123"


def test_alert_payload_metadata_stored() -> None:
    p = _minimal_payload(metadata={"region": "us-east-1", "env": "prod"})
    assert p.metadata["region"] == "us-east-1"
    assert p.metadata["env"] == "prod"


def test_alert_payload_explicit_timestamp() -> None:
    ts = datetime(2026, 5, 10, 3, 0, 1, tzinfo=UTC)
    p = _minimal_payload(timestamp=ts)
    assert p.timestamp == ts


def test_alert_payload_string_source_coerced() -> None:
    p = _minimal_payload(source="pagerduty")
    assert p.source is AlertSource.PAGERDUTY


def test_alert_payload_invalid_source_raises() -> None:
    with pytest.raises(ValidationError):
        _minimal_payload(source="newrelic")


def test_alert_payload_invalid_severity_raises() -> None:
    with pytest.raises(ValidationError):
        _minimal_payload(severity="urgent")


def test_alert_payload_json_round_trip() -> None:
    p = _minimal_payload(metadata={"key": "value"})
    reloaded = AlertPayload.model_validate_json(p.model_dump_json())
    assert reloaded.alert_id == p.alert_id
    assert reloaded.service == p.service
    assert reloaded.metadata == p.metadata


# ── AlertAck ──────────────────────────────────────────────────────────────────


def test_alert_ack_fields() -> None:
    ack = AlertAck(alert_id="alert-123", incident_id="inc-001")
    assert ack.alert_id == "alert-123"
    assert ack.incident_id == "inc-001"
    assert ack.received_at.tzinfo is not None


def test_alert_ack_explicit_received_at() -> None:
    ts = datetime(2026, 5, 10, 3, 0, 0, tzinfo=UTC)
    ack = AlertAck(alert_id="a", incident_id="i", received_at=ts)
    assert ack.received_at == ts


def test_alert_ack_missing_incident_id_raises() -> None:
    with pytest.raises(ValidationError):
        AlertAck(alert_id="a")  # type: ignore[call-arg]
