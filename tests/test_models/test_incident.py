"""Tests for sentinel.models.incident."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.incident import Incident, IncidentStatus, Severity, TimelineEntry


def _alert() -> AlertPayload:
    return AlertPayload(
        source=AlertSource.DATADOG,
        service="api-gateway",
        metric="error_rate",
        threshold=0.05,
        current_value=0.34,
        severity=AlertSeverity.CRITICAL,
    )


# ── Severity ──────────────────────────────────────────────────────────────────


def test_severity_values() -> None:
    assert Severity.P1 == "P1"
    assert Severity.P2 == "P2"
    assert Severity.P3 == "P3"
    assert Severity.P4 == "P4"


# ── IncidentStatus ────────────────────────────────────────────────────────────


def test_incident_status_values() -> None:
    assert IncidentStatus.TRIAGE == "triage"
    assert IncidentStatus.INVESTIGATING == "investigating"
    assert IncidentStatus.REMEDIATING == "remediating"
    assert IncidentStatus.PENDING_APPROVAL == "pending_approval"
    assert IncidentStatus.RESOLVED == "resolved"
    assert IncidentStatus.ESCALATED == "escalated"


# ── TimelineEntry ─────────────────────────────────────────────────────────────


def test_timeline_entry_defaults() -> None:
    entry = TimelineEntry(
        agent_name="triage",
        action="get_service_metadata",
        result_summary="api-gateway is critical tier, team-platform",
    )
    assert entry.agent_name == "triage"
    assert entry.timestamp.tzinfo is not None


def test_timeline_entry_explicit_timestamp() -> None:
    ts = datetime(2026, 5, 10, 3, 0, 0, tzinfo=UTC)
    entry = TimelineEntry(
        agent_name="log_analyst",
        action="fetch_logs",
        result_summary="Found 23 NullPointerExceptions",
        timestamp=ts,
    )
    assert entry.timestamp == ts


def test_timeline_entry_missing_fields_raises() -> None:
    with pytest.raises(ValidationError):
        TimelineEntry(agent_name="triage")  # type: ignore[call-arg]


# ── Incident ──────────────────────────────────────────────────────────────────


def test_incident_defaults() -> None:
    inc = Incident(alert=_alert())
    assert inc.id.startswith("inc-")
    assert len(inc.id) == len("inc-") + 8
    assert inc.status is IncidentStatus.TRIAGE
    assert inc.severity is None
    assert inc.affected_service is None
    assert inc.timeline == []
    assert inc.triage_result is None
    assert inc.log_analysis is None
    assert inc.deploy_correlation is None
    assert inc.remediation_plan is None
    assert inc.comms_summary is None
    assert inc.resolved_at is None
    assert inc.created_at.tzinfo is not None


def test_incident_id_unique() -> None:
    i1 = Incident(alert=_alert())
    i2 = Incident(alert=_alert())
    assert i1.id != i2.id


def test_incident_alert_attached() -> None:
    alert = _alert()
    inc = Incident(alert=alert)
    assert inc.alert.service == "api-gateway"
    assert inc.alert.severity is AlertSeverity.CRITICAL


def test_incident_add_timeline_entry() -> None:
    inc = Incident(alert=_alert())
    entry = TimelineEntry(
        agent_name="triage",
        action="get_service_metadata",
        result_summary="done",
    )
    inc.timeline.append(entry)
    assert len(inc.timeline) == 1
    assert inc.timeline[0].agent_name == "triage"


def test_incident_status_transition() -> None:
    inc = Incident(alert=_alert())
    inc.status = IncidentStatus.INVESTIGATING
    assert inc.status is IncidentStatus.INVESTIGATING
    inc.status = IncidentStatus.RESOLVED
    inc.resolved_at = datetime.now(UTC)
    assert inc.resolved_at is not None


def test_incident_severity_assignment() -> None:
    inc = Incident(alert=_alert())
    inc.severity = Severity.P1
    inc.affected_service = "api-gateway"
    assert inc.severity is Severity.P1
    assert inc.affected_service == "api-gateway"


def test_incident_result_fields_accept_dicts() -> None:
    inc = Incident(alert=_alert())
    inc.triage_result = {"severity": "P1", "service": "api-gateway"}
    inc.log_analysis = {"hypothesis": "NullPointer in AuthMiddleware"}
    assert inc.triage_result["severity"] == "P1"
    assert inc.log_analysis["hypothesis"] is not None


def test_incident_json_round_trip() -> None:
    inc = Incident(alert=_alert(), severity=Severity.P2)
    reloaded = Incident.model_validate_json(inc.model_dump_json())
    assert reloaded.id == inc.id
    assert reloaded.severity is Severity.P2
    assert reloaded.alert.service == inc.alert.service
