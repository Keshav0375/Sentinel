"""Tests for sentinel.models.deploy."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel.models.deploy import CorrelatedDeploy

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
