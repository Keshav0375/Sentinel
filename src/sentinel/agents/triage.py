"""Triage agent — severity classification and service identification."""

from __future__ import annotations

from typing import Any

from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.incident import TriageResult
from sentinel.providers import get_capabilities, resolve_model
from sentinel.tools.incident_search import make_incident_search_tool
from sentinel.tools.service_lookup import make_service_lookup_tool

TRIAGE_AGENT_NAME = "triage_agent"


def build_triage_agent(
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    *,
    model_string: str,
    settings: Settings,
) -> Agent[TriageResult]:
    """Create a configured Triage Agent with injected dependencies.

    The agent receives the alert payload as its input, calls
    ``get_service_metadata`` to enrich the service context, calls
    ``search_past_incidents`` to find similar resolved incidents, and
    returns a ``TriageResult`` with severity, affected service, duplicate
    flag, and recommended next action.

    Args:
        semantic_memory: Service map lookups for tier and oncall enrichment.
        episodic_memory: Past-incident similarity search for dedup and hints.
        model_string: Provider/model string (e.g. 'groq/llama-3.1-8b-instant').
            Passed to resolve_model — determines both provider and model.
        settings: Validated Settings instance for API key lookup.

    Returns:
        Configured Agent that produces a ``TriageResult`` as structured output
        when the provider supports it, or plain text otherwise.
    """
    llm = resolve_model(model_string, settings)
    caps = get_capabilities(model_string)

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = TriageResult
    if caps.default_model_settings:
        kwargs["model_settings"] = caps.default_model_settings

    return Agent(
        name=TRIAGE_AGENT_NAME,
        instructions=load_prompt("triage.txt"),
        tools=[
            make_service_lookup_tool(semantic_memory),
            make_incident_search_tool(episodic_memory),
        ],
        model=llm,
        **kwargs,
    )
