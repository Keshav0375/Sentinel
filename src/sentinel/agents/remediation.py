"""Remediation agent — drafts a rollback spec or an escalation; it executes nothing."""

from __future__ import annotations

from typing import Any

from agents import Agent, Tool

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.models.remediation import RemediationPlan
from sentinel.providers import Role, model_for, role_capabilities
from sentinel.tools.remediation_tools import make_remediation_tools

REMEDIATION_AGENT_NAME = "remediation_agent"
# The §4.7 role whose model pair this agent runs on (providers/router.py).
_ROLE: Role = "resolution"


def build_remediation_agent(
    *,
    settings: Settings,
) -> Agent[RemediationPlan]:
    """Create a configured Remediation Agent.

    The agent receives root cause findings (hypothesis, suspect deploy, log
    evidence) from the orchestrator, decides between rollback and escalation,
    and drafts the artifact with ``prepare_rollback_spec`` or
    ``format_escalation``. Both tools are output-only.

    HITL lives outside the backend (backend.md §3.3): GitHub Actions turns the
    rollback spec into a revert PR on Sentinel-deployment, and a human merges or
    closes it. The agent has no tool that opens, merges or approves anything.

    Args:
        settings: Validated Settings — selects the primary provider and supplies its key.

    Returns:
        Configured Agent that produces a ``RemediationPlan`` as structured output
        when the provider supports it.
    """
    llm, _ = model_for(_ROLE, settings)
    caps = role_capabilities(_ROLE, settings)

    # make_remediation_tools() returns [prepare_rollback_spec, format_escalation]
    tools: list[Tool] = [*make_remediation_tools()]

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = RemediationPlan

    return Agent(
        name=REMEDIATION_AGENT_NAME,
        instructions=load_prompt("remediation.txt"),
        tools=tools,
        model=llm,
        **kwargs,
    )
