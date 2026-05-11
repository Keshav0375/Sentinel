"""Tests for sentinel.models.deploy."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel.models.deploy import Deploy, DeployCorrelation


def _deploy(deploy_id: str = "deploy-abc123", ts: datetime | None = None) -> Deploy:
    return Deploy(
        id=deploy_id,
        service="api-gateway",
        timestamp=ts or datetime(2026, 5, 10, 2, 45, 0, tzinfo=UTC),
        author="dev-bot",
        commit_sha="a1b2c3d",
        files_changed=3,
    )


# ── Deploy ────────────────────────────────────────────────────────────────────


def test_deploy_required_fields() -> None:
    d = _deploy()
    assert d.id == "deploy-abc123"
    assert d.service == "api-gateway"
    assert d.author == "dev-bot"
    assert d.commit_sha == "a1b2c3d"
    assert d.files_changed == 3
    assert d.description == ""


def test_deploy_with_description() -> None:
    d = Deploy(
        id="deploy-xyz",
        service="user-service",
        author="keshav",
        commit_sha="x1y2z3a",
        files_changed=1,
        description="fix: null check in auth middleware",
    )
    assert d.description == "fix: null check in auth middleware"


def test_deploy_default_timestamp_is_utc() -> None:
    d = Deploy(
        id="d1",
        service="svc",
        author="bot",
        commit_sha="abc",
        files_changed=0,
    )
    assert d.timestamp.tzinfo is not None


def test_deploy_missing_required_raises() -> None:
    with pytest.raises(ValidationError):
        Deploy(id="d1", service="svc")  # type: ignore[call-arg]


def test_deploy_json_round_trip() -> None:
    d = _deploy()
    reloaded = Deploy.model_validate_json(d.model_dump_json())
    assert reloaded.id == d.id
    assert reloaded.commit_sha == d.commit_sha
    assert reloaded.files_changed == d.files_changed


# ── DeployCorrelation ─────────────────────────────────────────────────────────


def test_deploy_correlation_defaults() -> None:
    corr = DeployCorrelation(evidence="no recent deploys found")
    assert corr.recent_deploys == []
    assert corr.suspect_deploy is None
    assert corr.confidence == 0.0


def test_deploy_correlation_with_suspect() -> None:
    culprit = _deploy("deploy-abc123")
    distractor = _deploy("deploy-xyz789", datetime(2026, 5, 9, 14, 0, 0, tzinfo=UTC))
    corr = DeployCorrelation(
        recent_deploys=[culprit, distractor],
        suspect_deploy=culprit,
        confidence=0.92,
        evidence="deploy-abc123 landed 15 min before error spike; 3 files changed in auth path",
    )
    assert len(corr.recent_deploys) == 2
    assert corr.suspect_deploy is not None
    assert corr.suspect_deploy.id == "deploy-abc123"
    assert corr.confidence == 0.92


def test_deploy_correlation_json_round_trip() -> None:
    culprit = _deploy()
    corr = DeployCorrelation(
        recent_deploys=[culprit],
        suspect_deploy=culprit,
        confidence=0.85,
        evidence="timing correlation",
    )
    reloaded = DeployCorrelation.model_validate_json(corr.model_dump_json())
    assert reloaded.confidence == corr.confidence
    assert reloaded.suspect_deploy is not None
    assert reloaded.suspect_deploy.id == culprit.id
