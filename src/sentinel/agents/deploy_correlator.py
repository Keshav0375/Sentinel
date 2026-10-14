"""Deploy correlator agent — recent deploy lookup and suspicion ranking."""

from __future__ import annotations

from typing import Any

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.models.deploy import DeployCorrelation
from sentinel.providers import get_capabilities, resolve_model
from sentinel.tools.deploy_checker import make_deploy_checker_tool

DEPLOY_CORRELATOR_AGENT_NAME = "deploy_correlator_agent"


def build_deploy_correlator_agent(
    pool: asyncpg.Pool | None,
    *,
    model_string: str,
    settings: Settings,
) -> Agent[DeployCorrelation]:
    """Create a configured Deploy Correlator Agent with injected dependencies.

    The agent receives a service name and incident timestamp, calls
    ``get_deploy_details`` to enumerate recent deployment events, ranks
    them by suspicion (timing proximity, change scope, module overlap with
    log evidence), and returns a ``DeployCorrelation`` identifying the most
    likely culprit deploy along with a confidence score and evidence summary.

    Args:
        pool: asyncpg pool holding the ``deployments`` table. ``None`` (no
            ``DATABASE_URL``) makes ``get_deploy_details`` return a structured
            "unavailable" failure at call time rather than crashing at construction.
        model_string: Provider/model string (e.g. 'groq/llama-3.1-8b-instant').
        settings: Validated Settings instance for API key lookup; also supplies the
            GitHub token ``get_deploy_details`` reads at call time for PR enrichment.

    Returns:
        Configured Agent that produces a ``DeployCorrelation`` as structured
        output when the provider supports it, or plain text otherwise.
    """
    llm = resolve_model(model_string, settings)
    caps = get_capabilities(model_string)

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = DeployCorrelation
    if caps.default_model_settings:
        kwargs["model_settings"] = caps.default_model_settings

    return Agent(
        name=DEPLOY_CORRELATOR_AGENT_NAME,
        instructions=load_prompt("deploy_correlator.txt"),
        tools=[
            make_deploy_checker_tool(pool, settings),
        ],
        model=llm,
        **kwargs,
    )
