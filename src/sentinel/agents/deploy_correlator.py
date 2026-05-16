"""Deploy correlator agent — recent deploy lookup and suspicion ranking."""

from __future__ import annotations

from typing import Any

from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.generator.scenarios import Scenario
from sentinel.models.deploy import DeployCorrelation
from sentinel.providers import get_capabilities, resolve_model
from sentinel.tools.deploy_checker import make_deploy_checker_tool

DEPLOY_CORRELATOR_AGENT_NAME = "deploy_correlator_agent"


def build_deploy_correlator_agent(
    scenario: Scenario | None,
    *,
    model_string: str,
    settings: Settings,
) -> Agent[DeployCorrelation]:
    """Create a configured Deploy Correlator Agent with injected dependencies.

    The agent receives a service name and incident timestamp, calls
    ``list_recent_deploys`` to enumerate recent deployment events, ranks
    them by suspicion (timing proximity, change scope, module overlap with
    log evidence), and returns a ``DeployCorrelation`` identifying the most
    likely culprit deploy along with a confidence score and evidence summary.

    Args:
        scenario: Active scenario supplying synthetic deploy history in the
            MVP. ``None`` causes ``list_recent_deploys`` to return an error
            response at call time rather than crashing at construction.
        model_string: Provider/model string (e.g. 'groq/llama-3.1-8b-instant').
        settings: Validated Settings instance for API key lookup.

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
            make_deploy_checker_tool(scenario),
        ],
        model=llm,
        **kwargs,
    )
