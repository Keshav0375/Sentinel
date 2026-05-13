"""Log analyst agent — log fetching and pattern analysis."""

from __future__ import annotations

from agents import Agent
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from sentinel.agents.loader import load_prompt
from sentinel.generator.scenarios import Scenario
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.log_entry import LogAnalysis
from sentinel.tools.log_fetcher import make_log_fetcher_tool
from sentinel.tools.service_lookup import make_service_lookup_tool

LOG_ANALYST_AGENT_NAME = "log_analyst_agent"


def build_log_analyst_agent(
    semantic_memory: SemanticMemory,
    scenario: Scenario | None,
    *,
    groq_client: AsyncOpenAI,
    model_name: str = "llama-3.3-70b-versatile",
) -> Agent[LogAnalysis]:
    """Create a configured Log Analyst Agent with injected dependencies.

    The agent receives a service name and time window, fetches logs for that
    service via ``fetch_logs``, enriches context with ``get_service_metadata``
    when dependency information is needed, and returns a ``LogAnalysis`` with
    error patterns, an anomaly summary, key evidence lines, and a one-sentence
    root cause hypothesis.

    Args:
        semantic_memory: Service map lookups used to understand dependency
            topology when errors may propagate from upstream services.
        scenario: Active scenario supplying synthetic log data in the MVP.
            ``None`` causes ``fetch_logs`` to return an error response, which
            the agent will surface in its analysis.
        groq_client: Pre-configured AsyncOpenAI client pointed at Groq's API.
        model_name: Groq model to use. Defaults to llama-3.3-70b-versatile
            (the capable analysis model, equivalent to gpt-4o in context and
            reasoning quality).

    Returns:
        Configured Agent that produces a ``LogAnalysis`` as structured output.
    """
    llm = OpenAIChatCompletionsModel(model=model_name, openai_client=groq_client)

    return Agent(
        name=LOG_ANALYST_AGENT_NAME,
        instructions=load_prompt("log_analyst.txt"),
        tools=[
            make_log_fetcher_tool(scenario),
            make_service_lookup_tool(semantic_memory),
        ],
        output_type=LogAnalysis,
        model=llm,
    )
