"""Tests for sentinel.models.remediation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinel.models.remediation import (
    ApprovalRequest,
    ApprovalResult,
    ApprovalStatus,
    RemediationAction,
    RemediationPlan,
    RiskLevel,
    RollbackPR,
)

# ── Enums ─────────────────────────────────────────────────────────────────────


def test_remediation_action_values() -> None:
    assert RemediationAction.ROLLBACK == "rollback"
    assert RemediationAction.HOTFIX == "hotfix"
    assert RemediationAction.SCALE == "scale"
    assert RemediationAction.RESTART == "restart"
    assert RemediationAction.ESCALATE == "escalate"


def test_risk_level_values() -> None:
    assert RiskLevel.HIGH == "high"
    assert RiskLevel.MEDIUM == "medium"
    assert RiskLevel.LOW == "low"


def test_approval_status_values() -> None:
    assert ApprovalStatus.APPROVED == "approved"
    assert ApprovalStatus.REJECTED == "rejected"
    assert ApprovalStatus.TIMEOUT == "timeout"


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


# ── ApprovalRequest ───────────────────────────────────────────────────────────


def test_approval_request_fields() -> None:
    req = ApprovalRequest(
        action="rollback deploy-abc123 on api-gateway",
        risk_level=RiskLevel.HIGH,
        evidence_summary="deploy-abc123 correlates with 7x error spike; null ref in logs",
        proposed_by="remediation_agent",
    )
    assert req.risk_level is RiskLevel.HIGH
    assert req.proposed_by == "remediation_agent"


def test_approval_request_string_risk_coerced() -> None:
    req = ApprovalRequest(
        action="restart api-gateway",
        risk_level="medium",  # type: ignore[arg-type]
        evidence_summary="OOM condition",
        proposed_by="remediation_agent",
    )
    assert req.risk_level is RiskLevel.MEDIUM


def test_approval_request_invalid_risk_raises() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequest(
            action="x",
            risk_level="extreme",  # type: ignore[arg-type]
            evidence_summary="y",
            proposed_by="z",
        )


# ── ApprovalResult ────────────────────────────────────────────────────────────


def test_approval_result_approved() -> None:
    result = ApprovalResult(status=ApprovalStatus.APPROVED, reviewer="keshav")
    assert result.status is ApprovalStatus.APPROVED
    assert result.comment is None


def test_approval_result_rejected_with_comment() -> None:
    result = ApprovalResult(
        status=ApprovalStatus.REJECTED,
        reviewer="keshav",
        comment="Too risky during peak hours, escalate to on-call instead",
    )
    assert result.status is ApprovalStatus.REJECTED
    assert result.comment is not None


def test_approval_result_empty_reviewer_raises() -> None:
    with pytest.raises(ValidationError):
        ApprovalResult(status=ApprovalStatus.APPROVED, reviewer="  ")


def test_approval_result_json_round_trip() -> None:
    result = ApprovalResult(
        status=ApprovalStatus.APPROVED,
        reviewer="keshav",
        comment="looks good",
    )
    reloaded = ApprovalResult.model_validate_json(result.model_dump_json())
    assert reloaded.status is ApprovalStatus.APPROVED
    assert reloaded.reviewer == "keshav"
