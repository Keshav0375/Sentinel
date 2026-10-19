"""Tests for sentinel.models.pipeline — PipelineInput and PipelineResult (phase 4)."""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.pipeline import (
    DEFAULT_SIGNAL_TYPE,
    SIGNAL_TYPES,
    PipelineInput,
    PipelineResult,
)
from sentinel.models.tools import EscalationContext, RollbackSpec, TargetDeploy

_REPO = Path(__file__).resolve().parents[2]


def _alert(**metadata: Any) -> AlertPayload:
    return AlertPayload(
        source=AlertSource.DATADOG,
        service="sentinel-watchtower",
        metric="synthetics.http.failed",
        threshold=1,
        current_value=3,
        severity=AlertSeverity.CRITICAL,
        metadata=metadata,
    )


def _spec(incident_id: str) -> RollbackSpec:
    return RollbackSpec(
        incident_id=incident_id,
        root_cause="pr-42 broke GET /",
        evidence_summary="checks fail since pr-42",
        confidence=0.9,
        justification="the previous version passed",
        target_deploy=TargetDeploy(
            pr_number=42, commit_sha="a1b2c3d", deployed_at=datetime(2026, 10, 15, tzinfo=UTC)
        ),
    )


def _escalation() -> EscalationContext:
    return EscalationContext(
        hypothesis="unclear", evidence=["a", "b"], confidence=0.3, missing=["the logs"]
    )


def _common(incident_id: str) -> dict[str, Any]:
    return {
        "incident_id": incident_id,
        "root_cause": "r",
        "confidence": 0.5,
        "reflexion_loops": 0,
        "tool_calls_used": 0,
        "tokens_used": 0,
    }


# ── PipelineInput ─────────────────────────────────────────────────────────────


def test_signal_types_are_the_two_cases() -> None:
    assert SIGNAL_TYPES == ("deploy_failure", "runtime_error")
    assert DEFAULT_SIGNAL_TYPE == "runtime_error"


def test_from_alert_defaults_to_the_full_pipeline_and_a_fresh_correlation_id() -> None:
    incident_id = str(uuid.uuid4())
    request = PipelineInput.from_alert(incident_id, _alert())
    assert request.incident_id == incident_id
    assert request.signal_type == "runtime_error"
    assert uuid.UUID(request.correlation_id).version == 4


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("deploy_failure", "deploy_failure"),
        ("  DEPLOY_FAILURE ", "deploy_failure"),
        ("runtime_error", "runtime_error"),
        ("deploy-failure", "runtime_error"),
        ("rollback_now", "runtime_error"),
        (1, "runtime_error"),
        (None, "runtime_error"),
    ],
)
def test_from_alert_reads_the_signal_type_from_metadata(value: object, expected: str) -> None:
    request = PipelineInput.from_alert(str(uuid.uuid4()), _alert(signal_type=value))
    assert request.signal_type == expected


def test_from_alert_keeps_a_uuid_correlation_id_and_replaces_anything_else() -> None:
    correlation_id = str(uuid.uuid4())
    kept = PipelineInput.from_alert(str(uuid.uuid4()), _alert(correlation_id=correlation_id))
    assert kept.correlation_id == correlation_id
    replaced = PipelineInput.from_alert(str(uuid.uuid4()), _alert(correlation_id="corr-1"))
    assert replaced.correlation_id != "corr-1" and uuid.UUID(replaced.correlation_id)


def test_incident_and_correlation_ids_must_be_uuids() -> None:
    with pytest.raises(ValidationError, match="not a UUID"):
        PipelineInput.from_alert("eval-bad_deploy_01-abc", _alert())
    with pytest.raises(ValidationError, match="not a UUID"):
        PipelineInput(
            incident_id=str(uuid.uuid4()),
            alert=_alert(),
            signal_type="runtime_error",
            correlation_id="nope",
        )
    with pytest.raises(ValidationError):
        PipelineInput(
            incident_id=str(uuid.uuid4()),
            alert=_alert(),
            signal_type="maybe",  # type: ignore[arg-type]
            correlation_id=str(uuid.uuid4()),
        )


def test_ids_are_normalised() -> None:
    raw = uuid.uuid4()
    request = PipelineInput.from_alert(raw.hex.upper(), _alert())
    assert request.incident_id == str(raw)


# ── PipelineResult ────────────────────────────────────────────────────────────


def test_rollback_result_exposes_the_9_3_outputs() -> None:
    incident_id = str(uuid.uuid4())
    result = PipelineResult(
        status="rollback_proposed",
        resolution_type="rollback",
        rollback_spec=_spec(incident_id),
        **_common(incident_id),
    )
    assert result.target_sha == "a1b2c3d"
    assert result.target_deploy == "a1b2c3d"
    assert result.evidence_summary == "checks fail since pr-42"
    assert result.models_used == []


def test_escalated_result_exposes_the_9_3_outputs() -> None:
    incident_id = str(uuid.uuid4())
    result = PipelineResult(
        status="escalated",
        resolution_type="escalated",
        escalation=_escalation(),
        **_common(incident_id),
    )
    assert result.target_sha is None
    assert result.target_deploy is None
    assert result.evidence_summary == "a; b"


@pytest.mark.parametrize("outcome", ["rollback", "escalated"])
def test_the_9_3_outputs_are_serialized_and_round_trip(outcome: str) -> None:
    """target_deploy, target_sha and evidence_summary are in model_dump (what
    pipeline_result and GET /incidents/{id} carry), and a dump validates back."""
    incident_id = str(uuid.uuid4())
    if outcome == "rollback":
        result = PipelineResult(
            status="rollback_proposed",
            resolution_type="rollback",
            rollback_spec=_spec(incident_id),
            **_common(incident_id),
        )
        expected = {
            "target_deploy": "a1b2c3d",
            "target_sha": "a1b2c3d",
            "evidence_summary": "checks fail since pr-42",
        }
    else:
        result = PipelineResult(
            status="escalated",
            resolution_type="escalated",
            escalation=_escalation(),
            **_common(incident_id),
        )
        expected = {"target_deploy": None, "target_sha": None, "evidence_summary": "a; b"}
    dumped = result.model_dump(mode="json")
    assert {key: dumped[key] for key in expected} == expected
    assert json.loads(result.model_dump_json())["evidence_summary"] == expected["evidence_summary"]
    assert PipelineResult.model_validate(dumped) == result


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "rollback_proposed", "resolution_type": "escalated", "spec": True},
        {"status": "rollback_proposed", "resolution_type": "rollback"},
        {"status": "rollback_proposed", "resolution_type": "rollback", "spec": True, "esc": True},
        {"status": "escalated", "resolution_type": "rollback", "esc": True},
        {"status": "escalated", "resolution_type": "escalated"},
        {"status": "escalated", "resolution_type": "escalated", "spec": True, "esc": True},
        {"status": "pending_approval", "resolution_type": "rollback", "spec": True},
        {"status": "resolved", "resolution_type": "rollback", "spec": True},
    ],
)
def test_result_must_carry_exactly_one_consistent_outcome(fields: dict[str, Any]) -> None:
    incident_id = str(uuid.uuid4())
    with pytest.raises(ValidationError):
        PipelineResult(
            status=fields["status"],
            resolution_type=fields["resolution_type"],
            rollback_spec=_spec(incident_id) if fields.get("spec") else None,
            escalation=_escalation() if fields.get("esc") else None,
            **_common(incident_id),
        )


def test_result_counters_are_non_negative() -> None:
    incident_id = str(uuid.uuid4())
    with pytest.raises(ValidationError):
        PipelineResult(
            status="escalated",
            resolution_type="escalated",
            escalation=_escalation(),
            **{**_common(incident_id), "tool_calls_used": -1},
        )


# ── Vocabulary ────────────────────────────────────────────────────────────────


def test_pending_approval_is_gone_from_the_repo() -> None:
    """Terminal statuses are rollback_proposed | escalated (decision 2026-10-15): no file
    the repo tracks names an approval state (this test's own pattern excepted)."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_REPO, capture_output=True, check=True
    ).stdout.split(b"\0")
    pattern = re.compile(r"pending[_ ]approval", re.IGNORECASE)
    this_file = Path(__file__).resolve()
    offenders: list[str] = []
    for name in filter(None, tracked):
        path = _REPO / name.decode()
        if path.resolve() == this_file or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary (images)
        offenders += [
            f"{name.decode()}:{n}"
            for n, line in enumerate(text.splitlines(), 1)
            if pattern.search(line)
        ]
    assert offenders == []
