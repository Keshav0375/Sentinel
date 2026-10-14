"""Remediation models — RemediationAction, RemediationPlan, RollbackPR.

Agents only *draft* (RemediationPlan, RollbackPR); they never execute anything.
The human gate is the revert PR on Sentinel-deployment, merged or closed on GitHub
(backend.md §3.3) — there is no approval model because there is no approval API.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class RemediationAction(StrEnum):
    """The type of remediation action the agent has selected."""

    ROLLBACK = "rollback"
    HOTFIX = "hotfix"
    SCALE = "scale"
    RESTART = "restart"
    ESCALATE = "escalate"


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

    This is an *artifact*, not an action. The backend never opens or merges a PR:
    GitHub Actions creates the revert PR and a human merges or closes it on GitHub.
    """

    title: str
    body: str
    deploy_id: str
    target_branch: str = "main"
