"""Deploy correlator agent — recent deploy lookup and suspicion ranking."""

from __future__ import annotations

from typing import Any

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.models.deploy import DeployCorrelation
from sentinel.providers import Role, model_for, role_capabilities
from sentinel.tools.deploy_checker import make_deploy_checker_tool

DEPLOY_CORRELATOR_AGENT_NAME = "deploy_correlator_agent"
# The §4.7 role whose model pair this agent runs on (providers/router.py).
_ROLE: Role = "analysis"


def build_deploy_correlator_agent(
    pool: asyncpg.Pool | None,
    *,
    settings: Settings,
) -> Agent[DeployCorrelation]:
    """Create a configured Deploy Correlator Agent with injected dependencies.

    The agent receives a service name and incident timestamp, calls
    ``get_deploy_details`` to enumerate recent deployment events, ranks
    them by suspicion (timing proximity, change scope, module overlap with
    log evidence), and returns a ``DeployCorrelation`` identifying the most
    likely culprit deploy along with a confidence score and evidence summary.
    Each deploy in it is a ``CorrelatedDeploy`` copied from a ``get_deploy_details``
    record, so ``suspect_deploy`` carries the real pr_number, commit_sha and
    deployed_at that ``prepare_rollback_spec`` takes as ``target_deploy``.

    Args:
        pool: asyncpg pool holding the ``deployments`` table. ``None`` (no
            ``DATABASE_URL``) makes ``get_deploy_details`` return a structured
            "unavailable" failure at call time rather than crashing at construction.
        settings: Validated Settings — selects the primary provider and supplies its key;
            also the GitHub token ``get_deploy_details`` reads at call time for PR
            enrichment.

    Returns:
        Configured Agent that produces a ``DeployCorrelation`` as structured
        output when the provider supports it, or plain text otherwise.
    """
    llm, _ = model_for(_ROLE, settings)
    caps = role_capabilities(_ROLE, settings)

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = DeployCorrelation

    return Agent(
        name=DEPLOY_CORRELATOR_AGENT_NAME,
        instructions=load_prompt("deploy_correlator.txt"),
        tools=[
            make_deploy_checker_tool(pool, settings),
        ],
        model=llm,
        **kwargs,
    )
