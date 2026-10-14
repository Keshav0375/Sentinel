"""Orchestrator agent — top-level coordinator and handoff router."""

from __future__ import annotations

from typing import Any, cast

from agents import Agent, Handoff, handoff

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.models.incident import IncidentSummary
from sentinel.providers import Role, model_for, role_capabilities

ORCHESTRATOR_AGENT_NAME = "incident_orchestrator"
# The §4.7 role whose model pair this agent runs on (providers/router.py).
_ROLE: Role = "orchestrator"

SPECIALIST_ORDER = (
    "triage_agent",
    "log_analyst_agent",
    "deploy_correlator_agent",
    "remediation_agent",
    "comms_agent",
)


def _make_handoffs(agents: list[Agent[Any]], *, strict: bool) -> list[Handoff[Any, Any]]:
    """Wrap agents in Handoff objects with provider-appropriate strict mode."""
    handoffs: list[Handoff[Any, Any]] = []
    for agent in agents:
        h = handoff(agent)
        h.strict_json_schema = strict
        handoffs.append(h)
    return handoffs


def build_orchestrator_agent(
    triage_agent: Agent[Any],
    log_analyst_agent: Agent[Any],
    deploy_correlator_agent: Agent[Any],
    remediation_agent: Agent[Any],
    comms_agent: Agent[Any],
    *,
    settings: Settings,
) -> Agent[IncidentSummary]:
    """Create a configured Orchestrator Agent wired to all five specialists.

    The orchestrator is the top-level coordinator. It receives the raw alert
    payload, routes through the five specialist agents in order via handoffs,
    enforces the 20 tool-call budget (through its system prompt), and returns
    an ``IncidentSummary`` describing the outcome of the full pipeline run.

    Args:
        triage_agent: Pre-built Triage Agent (severity classification).
        log_analyst_agent: Pre-built Log Analyst Agent (root cause analysis).
        deploy_correlator_agent: Pre-built Deploy Correlator (deploy ranking).
        remediation_agent: Pre-built Remediation Agent (drafts a rollback spec or escalation).
        comms_agent: Pre-built Comms Agent (writes the incident summary; sends nothing).
        settings: Validated Settings — selects the primary provider and supplies its key.

    Returns:
        Configured Orchestrator Agent that produces an ``IncidentSummary``
        after coordinating all specialist handoffs.
    """
    llm, _ = model_for(_ROLE, settings)
    caps = role_capabilities(_ROLE, settings)

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = IncidentSummary

    specialists = [
        triage_agent,
        log_analyst_agent,
        deploy_correlator_agent,
        remediation_agent,
        comms_agent,
    ]

    agent_handoffs = _make_handoffs(specialists, strict=caps.strict_schemas)
    return Agent(
        name=ORCHESTRATOR_AGENT_NAME,
        # orchestrator.txt states the tool-call budget as the literal 20: the default of
        # Settings.sentinel_max_tool_calls (§4.6). The prompt is not templated, so keep
        # the two in step (test_orchestrator_budget_matches_sentinel_max_tool_calls).
        instructions=load_prompt("orchestrator.txt"),
        handoffs=cast(list[Agent[Any] | Handoff[Any, Any]], agent_handoffs),
        model=llm,
        **kwargs,
    )
