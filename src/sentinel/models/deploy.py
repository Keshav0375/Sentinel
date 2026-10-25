"""Deploy models — CorrelatedDeploy."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CorrelatedDeploy(BaseModel):
    """A deploy the Analysis agent blames: identifying fields of a ``get_deploy_details`` row.

    A field-compatible subset of :class:`~sentinel.models.tools.DeployRecord` (same names
    and types), so a record validates into one unchanged, and ``pr_number``,
    ``commit_sha`` and ``deployed_at`` are exactly what ``prepare_rollback_spec`` takes
    as ``target_deploy``. Nothing defaults to "now": every value comes from the
    ``deployments`` row the tool returned.
    """

    pr_number: int | None = Field(
        description="Pull request that was deployed, copied from get_deploy_details; "
        "None for a direct push."
    )
    commit_sha: str = Field(description="Deployed commit, copied from get_deploy_details.")
    deployed_at: datetime = Field(
        description="When the deploy was recorded (UTC), copied from get_deploy_details."
    )
    version: str | None = Field(default=None, description='Deployed version, "pr-<n>-<sha>".')
    deploy_status: str = Field(description="succeeded | failed | rolled_back.")
    failed_stage: str | None = Field(
        default=None, description='Stage that failed (build, deploy, verify) or "none".'
    )
