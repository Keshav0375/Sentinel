"""Log analyst agent — log fetching and pattern analysis."""

from __future__ import annotations

from typing import Any

from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.generator.scenarios import Scenario
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.log_entry import LogAnalysis
from sentinel.providers import get_capabilities, resolve_model
from sentinel.tools.log_fetcher import make_log_fetcher_tool
from sentinel.tools.service_lookup import make_service_lookup_tool

LOG_ANALYST_AGENT_NAME = "log_analyst_agent"


def build_log_analyst_agent(
    semantic_memory: SemanticMemory,
    scenario: Scenario | None,
    *,
    model_string: str,
    settings: Settings,
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
        model_string: Provider/model string (e.g. 'groq/llama-3.3-70b-versatile').
        settings: Validated Settings instance for API key lookup.

    Returns:
        Configured Agent that produces a ``LogAnalysis`` as structured output
        when the provider supports it, or plain text otherwise.
    """
    llm = resolve_model(model_string, settings)
    caps = get_capabilities(model_string)

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = LogAnalysis
    if caps.default_model_settings:
        kwargs["model_settings"] = caps.default_model_settings

    return Agent(
        name=LOG_ANALYST_AGENT_NAME,
        instructions=load_prompt("log_analyst.txt"),
        tools=[
            make_log_fetcher_tool(scenario),
            make_service_lookup_tool(semantic_memory),
        ],
        model=llm,
        **kwargs,
    )
