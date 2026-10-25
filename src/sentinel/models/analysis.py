"""Analysis model — the Analysis agent's root-cause hypothesis and its evidence (§4.2).

The ``analysis`` agent merges the Phase-1 log analyst and deploy correlator (decision
2026-10-15): it reads the service's Datadog evidence (``fetch_logs``) and its deploy history
(``get_deploy_details``) and answers with one :class:`AnalysisResult`. The orchestrator
hands that to the ``reflexion`` critique; nothing here acts on the world.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sentinel.models.deploy import CorrelatedDeploy

__all__ = ["AnalysisResult"]


class AnalysisResult(BaseModel):
    """Output of the ``analysis`` agent: one hypothesis and the evidence behind it.

    ``suspect_deploy`` is copied from a ``get_deploy_details`` record (never invented), so
    its pr_number, commit_sha and deployed_at are what ``prepare_rollback_spec`` takes as
    ``target_deploy``. It is None when no deploy is convincingly correlated.
    """

    hypothesis: str = Field(description="One sentence: what broke, when, and why.")
    evidence: list[str] = Field(
        default=[], description="Evidence items, each citing the tool result it comes from."
    )
    error_patterns: list[str] = Field(
        default=[], description="Recurring failure patterns (e.g. a failing assertion)."
    )
    suspect_deploy: CorrelatedDeploy | None = Field(
        default=None,
        description="The deploy the hypothesis blames, copied from get_deploy_details; "
        "null when no deploy is implicated.",
    )
