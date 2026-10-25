"""Tests for sentinel.models.remediation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinel.models import remediation
from sentinel.models.remediation import (
    RemediationAction,
    RemediationPlan,
    RollbackPR,
)

# ── Enums ─────────────────────────────────────────────────────────────────────


def test_remediation_action_values() -> None:
    assert RemediationAction.ROLLBACK == "rollback"
    assert RemediationAction.HOTFIX == "hotfix"
    assert RemediationAction.SCALE == "scale"
    assert RemediationAction.RESTART == "restart"
    assert RemediationAction.ESCALATE == "escalate"


# ── RemediationPlan ───────────────────────────────────────────────────────────


def test_remediation_plan_rollback() -> None:
    plan = RemediationPlan(
        action_type=RemediationAction.ROLLBACK,
        deploy_id="deploy-abc123",
        risk_assessment="Low risk — reverting to last known-good deploy",
        justification="deploy-abc123 introduced null ref correlated with error spike",
    )
    assert plan.action_type is RemediationAction.ROLLBACK
    assert plan.deploy_id == "deploy-abc123"
    assert plan.diff is None


def test_remediation_plan_hotfix() -> None:
    plan = RemediationPlan(
        action_type=RemediationAction.HOTFIX,
        diff=(
            "--- a/auth.py\n+++ b/auth.py\n@@ -10 +10 @@\n"
            "-  if cfg is None: pass\n+  if cfg is None: raise ConfigError()"
        ),
        risk_assessment="Minimal — one-line null check",
        justification="Null pointer introduced in deploy-abc123, hotfix is faster than rollback",
    )
    assert plan.action_type is RemediationAction.HOTFIX
    assert plan.diff is not None
    assert plan.deploy_id is None


def test_remediation_plan_escalate() -> None:
    plan = RemediationPlan(
        action_type=RemediationAction.ESCALATE,
        risk_assessment="Unknown — root cause unclear",
        justification="Log analysis inconclusive, need human investigation",
    )
    assert plan.action_type is RemediationAction.ESCALATE
    assert plan.deploy_id is None
    assert plan.diff is None


def test_remediation_plan_missing_required_raises() -> None:
    with pytest.raises(ValidationError):
        RemediationPlan(action_type=RemediationAction.ROLLBACK)  # type: ignore[call-arg]


def test_remediation_plan_json_round_trip() -> None:
    plan = RemediationPlan(
        action_type=RemediationAction.ROLLBACK,
        deploy_id="deploy-abc123",
        risk_assessment="low",
        justification="timing correlation",
    )
    reloaded = RemediationPlan.model_validate_json(plan.model_dump_json())
    assert reloaded.action_type is RemediationAction.ROLLBACK
    assert reloaded.deploy_id == "deploy-abc123"


# ── RollbackPR ────────────────────────────────────────────────────────────────


def test_rollback_pr_fields() -> None:
    pr = RollbackPR(
        title="revert: rollback deploy-abc123 (null ref in auth middleware)",
        body="This PR reverts deploy-abc123 which introduced a NullPointerException.",
        deploy_id="deploy-abc123",
    )
    assert pr.target_branch == "main"
    assert pr.deploy_id == "deploy-abc123"


def test_rollback_pr_custom_branch() -> None:
    pr = RollbackPR(
        title="revert: rollback deploy-xyz",
        body="body",
        deploy_id="deploy-xyz",
        target_branch="release/2.1",
    )
    assert pr.target_branch == "release/2.1"


# ── No approval models ────────────────────────────────────────────────────────


def test_no_approval_models_remain() -> None:
    """The revert PR on GitHub is the only gate (backend.md §3.3): no approval API types."""
    for name in ("ApprovalRequest", "ApprovalResult", "ApprovalStatus", "RiskLevel"):
        assert not hasattr(remediation, name), name
