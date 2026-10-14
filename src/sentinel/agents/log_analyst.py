"""Log analyst agent — log fetching and pattern analysis."""

from __future__ import annotations

from typing import Any

from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.log_entry import LogAnalysis
from sentinel.providers import Role, model_for, role_capabilities
from sentinel.tools.log_fetcher import make_log_fetcher_tool
from sentinel.tools.service_lookup import make_service_lookup_tool

LOG_ANALYST_AGENT_NAME = "log_analyst_agent"
# The §4.7 role whose model pair this agent runs on (providers/router.py).
_ROLE: Role = "analysis"


def build_log_analyst_agent(
    semantic_memory: SemanticMemory,
    *,
    settings: Settings,
) -> Agent[LogAnalysis]:
    """Create a configured Log Analyst Agent with injected dependencies.

    The agent receives a service name and time window, reads the service's Datadog
    evidence (synthetic check runs and CI deploy logs) via ``fetch_logs``, enriches
    context with ``get_service_metadata`` when dependency information is needed, and
    returns a ``LogAnalysis`` with error patterns, an anomaly summary, key evidence
    lines, and a one-sentence root cause hypothesis.

    Args:
        semantic_memory: Service map lookups used to understand dependency
            topology when errors may propagate from upstream services.
        settings: Validated Settings — selects the primary provider and supplies its key;
            also the Datadog keys and site ``fetch_logs`` reads at call time.

    Returns:
        Configured Agent that produces a ``LogAnalysis`` as structured output
        when the provider supports it, or plain text otherwise.
    """
    llm, _ = model_for(_ROLE, settings)
    caps = role_capabilities(_ROLE, settings)

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = LogAnalysis

    return Agent(
        name=LOG_ANALYST_AGENT_NAME,
        instructions=load_prompt("log_analyst.txt"),
        tools=[
            make_log_fetcher_tool(settings),
            make_service_lookup_tool(semantic_memory),
        ],
        model=llm,
        **kwargs,
    )
