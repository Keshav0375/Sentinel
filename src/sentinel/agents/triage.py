"""Triage agent — severity classification and service identification."""

from __future__ import annotations

from typing import Any, Final

from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.incident import TriageResult
from sentinel.providers import Role, model_for, role_capabilities
from sentinel.tools.incident_search import make_incident_search_tool
from sentinel.tools.service_lookup import make_service_lookup_tool

# The agent name is also its prompt file and LangFuse prompt name (decision 2026-10-15).
TRIAGE_AGENT_NAME: Final = "triage"
# The §4.7 role whose model pair this agent runs on (providers/router.py).
_ROLE: Role = "triage"


def build_triage_agent(
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    *,
    settings: Settings,
    instructions: str | None = None,
) -> Agent[Any]:
    """Create a configured Triage Agent with injected dependencies.

    The agent receives the alert payload as its input, calls
    ``get_service_metadata`` to enrich the service context, calls
    ``search_past_incidents`` to find similar resolved incidents, and
    returns a ``TriageResult`` with severity, affected service, duplicate
    flag, and recommended next action.

    Args:
        semantic_memory: Service map lookups for tier and oncall enrichment.
        episodic_memory: Past-incident similarity search for dedup and hints.
        settings: Validated Settings — selects the primary provider and supplies its key.
        instructions: The system prompt (``infra/tracing.load_prompt``, preamble included);
            None reads ``prompts/triage.txt``.

    Returns:
        Configured Agent that produces a ``TriageResult`` as structured output
        when the provider supports it, or its JSON as plain text otherwise.
    """
    llm, _ = model_for(_ROLE, settings)
    caps = role_capabilities(_ROLE, settings)

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = TriageResult

    return Agent(
        name=TRIAGE_AGENT_NAME,
        instructions=instructions or load_prompt(f"{TRIAGE_AGENT_NAME}.txt"),
        tools=[
            make_service_lookup_tool(semantic_memory),
            make_incident_search_tool(episodic_memory),
        ],
        model=llm,
        **kwargs,
    )
