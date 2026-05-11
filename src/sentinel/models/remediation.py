"""Remediation models — RemediationAction, RemediationPlan, RollbackPR,
ApprovalRequest, ApprovalResult.

These models encode the HITL safety contract: agents can only *draft* actions
(RemediationPlan, RollbackPR) and *request* approval (ApprovalRequest). They
never directly execute anything. ApprovalResult records the human's decision.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, field_validator


class RemediationAction(StrEnum):
    """The type of remediation action the agent has selected."""

    ROLLBACK = "rollback"
    HOTFIX = "hotfix"
    SCALE = "scale"
    RESTART = "restart"
    ESCALATE = "escalate"


class RiskLevel(StrEnum):
    """Perceived risk of a proposed remediation action.

    Used in ApprovalRequest so the human reviewer can judge urgency vs. risk.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ApprovalStatus(StrEnum):
    """Outcome of a HITL approval gate. Matches ARCHITECTURE.md §8."""

    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class RemediationPlan(BaseModel):
    """What the Remediation Agent proposes to do.

    Only one of deploy_id (rollback) or diff (hotfix) will be set,
    depending on action_type. Both are None for scale/restart/escalate.
    """

    action_type: RemediationAction
    deploy_id: str | None = None
    diff: str | None = None
    risk_assessment: str
    justification: str


class RollbackPR(BaseModel):
    """A drafted pull request that, once merged, reverts a bad deploy.

    This is an *artifact*, not an action. The human approves it via the HITL
    gate, then the orchestrator (in Phase 2) would merge it via the GitHub API.
    """

    title: str
    body: str
    deploy_id: str
    target_branch: str = "main"


class ApprovalRequest(BaseModel):
    """Sent to the HITL gate before any destructive action executes.

    Exactly matches the structure in ARCHITECTURE.md §8.
    """

    action: str
    risk_level: RiskLevel
    evidence_summary: str
    proposed_by: str


class ApprovalResult(BaseModel):
    """The human's decision returned by the HITL gate.

    reviewer is the identifier of whoever approved/rejected (username or
    "cli-input" for the MVP terminal prompt).
    """

    status: ApprovalStatus
    reviewer: str
    comment: str | None = None

    @field_validator("reviewer")
    @classmethod
    def reviewer_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reviewer must not be empty")
        return v
