"""Triage agent — severity classification and service identification."""

from __future__ import annotations

from agents import Agent
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from sentinel.agents.loader import load_prompt
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.incident import TriageResult
from sentinel.tools.incident_search import make_incident_search_tool
from sentinel.tools.service_lookup import make_service_lookup_tool

TRIAGE_AGENT_NAME = "triage_agent"


def build_triage_agent(
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    *,
    groq_client: AsyncOpenAI,
    model_name: str = "llama-3.1-8b-instant",
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
        groq_client: Pre-configured AsyncOpenAI client pointed at Groq's API.
        model_name: Groq model to use. Defaults to llama-3.1-8b-instant (fast
            classification model, equivalent to gpt-4o-mini in cost tier).

    Returns:
        Configured Agent that produces a ``TriageResult`` as structured output.
    """
    llm = OpenAIChatCompletionsModel(model=model_name, openai_client=groq_client)

    return Agent(
        name=TRIAGE_AGENT_NAME,
        instructions=load_prompt("triage.txt"),
        tools=[
            make_service_lookup_tool(semantic_memory),
            make_incident_search_tool(episodic_memory),
        ],
        output_type=TriageResult,
        model=llm,
    )
