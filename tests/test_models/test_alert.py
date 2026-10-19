"""Tests for sentinel.models.alert."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel.models.alert import (
    SIGNAL_TYPES,
    AlertPayload,
    AlertSeverity,
    AlertSource,
    IncidentAlert,
    IncidentContext,
    IncidentPayload,
    IncidentTags,
    PRDetails,
    parse_tags,
)

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


# ── IncidentPayload ───────────────────────────────────────────────────────────

_CID = "3f2b8c1e-4d5a-4b6c-9d7e-8f9a0b1c2d3e"


def _incident(**overrides: object) -> IncidentPayload:
    body: dict[str, object] = {"signal_type": "runtime_error", "correlation_id": _CID, "title": "t"}
    body.update(overrides)
    return IncidentPayload.model_validate(body)


@pytest.mark.parametrize("field", ["signal_type", "correlation_id", "title"])
def test_incident_payload_requires_three_fields(field: str) -> None:
    body: dict[str, object] = {"signal_type": "runtime_error", "correlation_id": _CID, "title": "t"}
    del body[field]
    with pytest.raises(ValidationError):
        IncidentPayload.model_validate(body)


def test_incident_payload_rejects_bad_values() -> None:
    with pytest.raises(ValidationError):
        _incident(signal_type="deploy-failure")
    with pytest.raises(ValidationError, match="not a UUID"):
        _incident(correlation_id="corr-1")
    with pytest.raises(ValidationError, match="blank"):
        _incident(title="   ")


def test_incident_payload_defaults_and_ignores_unknown_keys() -> None:
    p = _incident(surprise=1, context=None, tags=None)
    assert p.severity == "unknown"
    assert p.tags == IncidentTags()
    assert p.context == IncidentContext()
    assert p.raw_payload is None
    assert not hasattr(p, "surprise")


def test_correlation_id_normalised() -> None:
    assert _incident(correlation_id=_CID.upper().replace("-", "")).correlation_id == _CID


def test_title_is_stripped() -> None:
    assert _incident(title="  deploy failed ").title == "deploy failed"


def test_tags_string_parsed() -> None:
    tags = _incident(
        tags="service:sentinel-watchtower, deploy_status:failed,failed_stage:verify,"
        "version:a:b,env:dev,novalue,empty:,service:last"
    ).tags
    assert tags.service == "last"  # the last duplicate wins
    assert tags.deploy_status == "failed"
    assert tags.failed_stage == "verify"
    assert tags.version == "a:b"  # split on the FIRST colon only


def test_tags_object_ignores_unknown_keys() -> None:
    tags = _incident(tags={"service": "svc", "env": "dev"}).tags
    assert tags.service == "svc"
    assert not hasattr(tags, "env")


@pytest.mark.parametrize("bad", [["service:x"], 3])
def test_tags_other_types_rejected(bad: object) -> None:
    with pytest.raises(ValidationError):
        _incident(tags=bad)


def test_parse_tags_leaves_other_types_alone() -> None:
    assert parse_tags(None) == {}
    assert parse_tags(["a"]) == ["a"]


def test_signal_types() -> None:
    assert SIGNAL_TYPES == ("deploy_failure", "runtime_error")


# ── IncidentAlert ─────────────────────────────────────────────────────────────


def test_incident_alert_from_payload_fallbacks() -> None:
    alert = IncidentAlert.from_payload(_incident())
    assert alert.alert_id == "unknown"
    assert alert.service == "unknown"
    assert alert.severity == "unknown"
    assert alert.title == "t"


def test_incident_alert_from_payload_copies_fields() -> None:
    ts = datetime(2026, 10, 15, tzinfo=UTC)
    alert = IncidentAlert.from_payload(
        _incident(
            alert_id="a-1",
            dd_event_id="evt",
            source="datadog",
            severity="critical",
            tags={"service": "svc"},
            timestamp=ts,
        )
    )
    assert (alert.alert_id, alert.dd_event_id, alert.source) == ("a-1", "evt", "datadog")
    assert (alert.severity, alert.service, alert.timestamp) == ("critical", "svc", ts)
    assert alert.tags.service == "svc"


def test_pr_sha() -> None:
    assert IncidentAlert(title="t").pr_sha is None
    blank = IncidentContext(pr_details=PRDetails(sha="  "))
    assert IncidentAlert(title="t", context=blank).pr_sha is None
    padded = IncidentContext(pr_details=PRDetails(sha=" abc1234 "))
    assert IncidentAlert(title="t", context=padded).pr_sha == "abc1234"


def test_from_phase1_maps_fields() -> None:
    p = _minimal_payload(service="svc", metadata={})
    alert = IncidentAlert.from_phase1(p)
    assert alert.alert_id == p.alert_id
    assert alert.source == "datadog"
    assert alert.severity == "critical"
    assert alert.service == "svc"
    assert alert.tags.service == "svc"
    assert alert.timestamp == p.timestamp
    assert alert.title == f"{p.metric} {p.current_value} (threshold {p.threshold}) on svc"
    titled = IncidentAlert.from_phase1(_minimal_payload(metadata={"title": " Checkout down "}))
    assert titled.title == "Checkout down"
