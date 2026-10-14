"""Comms agent — a pure incident summariser: no tools, sends nothing."""

from __future__ import annotations

from typing import Any

from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.models.comms import SlackSummary
from sentinel.providers import Role, model_for, role_capabilities

COMMS_AGENT_NAME = "comms_agent"
# The §4.7 role whose model pair this agent runs on (providers/router.py). §4.7 has no comms
# row and backend 4.2 deletes this agent; until then it uses the constrained-text-generation
# pair.
_ROLE: Role = "pr_content_generator"


def build_comms_agent(
    *,
    settings: Settings,
) -> Agent[SlackSummary]:
    """Create a configured Comms Agent with injected dependencies.

    The agent receives the full incident context (triage result, log analysis,
    deploy correlation, remediation plan) and writes the summary itself,
    returned as a ``SlackSummary`` (message text and target channels). It has
    no tools and notifies no one: notifications are GitHub Actions jobs
    (backend.md §4.8).

    Args:
        settings: Validated Settings — selects the primary provider and supplies its key.

    Returns:
        Configured Agent that produces a ``SlackSummary`` as structured output
        when the provider supports it, or plain text otherwise.
    """
    llm, _ = model_for(_ROLE, settings)
    caps = role_capabilities(_ROLE, settings)

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = SlackSummary

    return Agent(
        name=COMMS_AGENT_NAME,
        instructions=load_prompt("comms.txt"),
        tools=[],
        model=llm,
        **kwargs,
    )
