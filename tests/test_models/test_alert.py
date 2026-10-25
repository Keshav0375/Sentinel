"""Tests for sentinel.models.alert."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel.models import alert as alert_module
from sentinel.models.alert import (
    SIGNAL_TYPES,
    TRUNCATED,
    IncidentAlert,
    IncidentContext,
    IncidentPayload,
    IncidentTags,
    PRDetails,
    parse_tags,
)
from sentinel.models.pipeline import PipelineInput

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


def test_from_payload_bounds_recent_logs() -> None:
    logs = [f"log {i}" for i in range(60)]
    alert = IncidentAlert.from_payload(_incident(context={"recent_logs": logs}))
    assert len(alert.context.recent_logs) == 51
    assert alert.context.recent_logs[:50] == logs[:50]
    assert alert.context.recent_logs[-1] == "[10 more log entries truncated]"


def test_from_payload_bounds_service_metadata() -> None:
    metadata = {f"k{i}": i for i in range(55)}
    alert = IncidentAlert.from_payload(_incident(context={"service_metadata": metadata}))
    bounded = alert.context.service_metadata
    assert bounded is not None
    assert len(bounded) == 51
    assert bounded["_truncated"] == "5 more keys truncated"
    assert bounded["k0"] == 0 and "k54" not in bounded


def test_from_payload_truncates_a_long_item() -> None:
    alert = IncidentAlert.from_payload(_incident(context={"recent_logs": [{"msg": "y" * 5000}]}))
    (item,) = alert.context.recent_logs
    assert isinstance(item, str)
    assert len(item) == 2000 + len(TRUNCATED)
    assert item.endswith(TRUNCATED)


def test_from_payload_small_context_unchanged() -> None:
    context = {
        "recent_logs": ["a", {"b": 1}],
        "service_metadata": {"x": 1, "y": "two"},
        "pr_details": {"number": 7, "sha": "abc1234", "files_changed": ["f.py"]},
    }
    payload = _incident(context=context)
    alert = IncidentAlert.from_payload(payload)
    assert alert.context == payload.context
    assert alert.pr_sha == "abc1234"


def test_pr_details_branch_survives_the_alert() -> None:
    context = {"pr_details": {"number": 7, "sha": "abc1234", "branch": "runtime/01"}}
    alert = IncidentAlert.from_payload(_incident(context=context))
    assert alert.context.pr_details is not None
    assert alert.context.pr_details.branch == "runtime/01"
    bare = IncidentAlert.from_payload(_incident(context={"pr_details": {"number": 7}}))
    assert bare.context.pr_details is not None
    assert bare.context.pr_details.branch is None


def test_pr_sha() -> None:
    assert IncidentAlert(title="t").pr_sha is None
    blank = IncidentContext(pr_details=PRDetails(sha="  "))
    assert IncidentAlert(title="t", context=blank).pr_sha is None
    padded = IncidentContext(pr_details=PRDetails(sha=" abc1234 "))
    assert IncidentAlert(title="t", context=padded).pr_sha == "abc1234"


# ── PRDetails.branch (the eval label) ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("deployfail/03", "deployfail/03"),
        (" runtime/01 ", "runtime/01"),
        ("a.b_c-D/9", "a.b_c-D/9"),
        ("x" * 100, "x" * 100),
    ],
)
def test_branch_keeps_a_valid_name(value: str, expected: str) -> None:
    assert PRDetails(branch=value).branch == expected


@pytest.mark.parametrize("value", ["", "  ", "x" * 101, "a b", "run;rm -rf", "ü/01", "a\nb"])
def test_branch_drops_an_invalid_name_without_rejecting(value: str) -> None:
    assert PRDetails(branch=value).branch is None
    payload = IncidentPayload(
        signal_type="runtime_error",
        correlation_id=_CID,
        title="t",
        context=IncidentContext.model_validate({"pr_details": {"branch": value, "number": 1}}),
    )
    assert payload.context.pr_details is not None
    assert payload.context.pr_details.branch is None
    assert payload.context.pr_details.number == 1


def test_agent_view_drops_only_the_branch() -> None:
    pr = PRDetails(
        number=31, sha="abc1234", author="k", branch="deployfail/03", files_changed=["a"]
    )
    alert = IncidentAlert(title="t", context=IncidentContext(pr_details=pr))
    view = alert.agent_view()
    assert view.context.pr_details is not None
    assert view.context.pr_details.branch is None
    assert view.context.pr_details.number == 31
    assert view.context.pr_details.sha == "abc1234"
    assert view.context.pr_details.author == "k"
    assert view.context.pr_details.files_changed == ["a"]
    assert alert.context.pr_details is not None
    assert alert.context.pr_details.branch == "deployfail/03"
    assert "deployfail/03" not in view.model_dump_json()
    assert "deployfail/03" not in view.model_dump_json(exclude_none=True)
    bare = IncidentAlert(title="t")
    assert bare.agent_view() is bare
    no_branch = IncidentAlert(title="t", context=IncidentContext(pr_details=PRDetails(number=1)))
    assert no_branch.agent_view() is no_branch


def test_phase1_models_are_gone() -> None:
    for name in ("AlertPayload", "AlertSource", "AlertSeverity"):
        assert not hasattr(alert_module, name)
    assert not hasattr(IncidentAlert, "from_phase1")
    assert not hasattr(PipelineInput, "from_alert")
    assert importlib.util.find_spec("sentinel.models.log_entry") is None
