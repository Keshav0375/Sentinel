"""Deploy correlator agent — recent deploy lookup and suspicion ranking."""

from __future__ import annotations

from agents import Agent
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from sentinel.agents.loader import load_prompt
from sentinel.generator.scenarios import Scenario
from sentinel.models.deploy import DeployCorrelation
from sentinel.tools.deploy_checker import make_deploy_checker_tool

DEPLOY_CORRELATOR_AGENT_NAME = "deploy_correlator_agent"


def build_deploy_correlator_agent(
    scenario: Scenario | None,
    *,
    groq_client: AsyncOpenAI,
    model_name: str = "llama-3.1-8b-instant",
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
        groq_client: Pre-configured AsyncOpenAI client pointed at Groq's API.
        model_name: Groq model to use. Defaults to llama-3.1-8b-instant (the
            fast classification model, equivalent to gpt-4o-mini — deploy
            correlation is a structured ranking task that does not require the
            larger analysis model).

    Returns:
        Configured Agent that produces a ``DeployCorrelation`` as structured
        output.
    """
    llm = OpenAIChatCompletionsModel(model=model_name, openai_client=groq_client)

    return Agent(
        name=DEPLOY_CORRELATOR_AGENT_NAME,
        instructions=load_prompt("deploy_correlator.txt"),
        tools=[
            make_deploy_checker_tool(scenario),
        ],
        output_type=DeployCorrelation,
        model=llm,
    )
