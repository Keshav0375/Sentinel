"""Tests for sentinel.models.deploy."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel.models.deploy import CorrelatedDeploy, Deploy


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


# ── CorrelatedDeploy ──────────────────────────────────────────────────────────


def test_correlated_deploy_requires_the_record_fields() -> None:
    with pytest.raises(ValidationError):
        CorrelatedDeploy.model_validate({"commit_sha": "a1b2c3d"})


def test_correlated_deploy_direct_push_has_no_pr() -> None:
    deploy = CorrelatedDeploy(
        pr_number=None,
        commit_sha="a1b2c3d",
        deployed_at=datetime(2026, 5, 10, tzinfo=UTC),
        deploy_status="succeeded",
    )
    assert deploy.pr_number is None
    assert deploy.version is None
