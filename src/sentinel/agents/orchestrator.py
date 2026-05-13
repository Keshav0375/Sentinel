"""Orchestrator agent — top-level coordinator and handoff router."""

from __future__ import annotations

from typing import Any

from agents import Agent
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from sentinel.agents.loader import load_prompt
from sentinel.models.incident import IncidentSummary

ORCHESTRATOR_AGENT_NAME = "incident_orchestrator"

# Expected workflow order — used in tests and as documentation.
SPECIALIST_ORDER = (
    "triage_agent",
    "log_analyst_agent",
    "deploy_correlator_agent",
    "remediation_agent",
    "comms_agent",
)


def build_orchestrator_agent(
    triage_agent: Agent[Any],
    log_analyst_agent: Agent[Any],
    deploy_correlator_agent: Agent[Any],
    remediation_agent: Agent[Any],
    comms_agent: Agent[Any],
    *,
    groq_client: AsyncOpenAI,
    model_name: str = "llama-3.3-70b-versatile",
) -> Agent[IncidentSummary]:
    """Create a configured Orchestrator Agent wired to all five specialists.

    The orchestrator is the top-level coordinator. It receives the raw alert
    payload, routes through the five specialist agents in order via handoffs,
    enforces the 15 tool-call budget (through its system prompt), and returns
    an ``IncidentSummary`` describing the outcome of the full pipeline run.

    Workflow order (governed by ``orchestrator.txt``):
    1. Triage → classify severity, identify affected service
    2. Log Analyst → fetch logs, produce root cause hypothesis
    3. Deploy Correlator → find suspect deploy, assign confidence score
    4. Remediation → draft fix artifact, obtain HITL approval
    5. Comms → produce Slack summary for #incidents

    Shortcuts:
    - Duplicate incident → skip to Comms
    - P4 severity → skip Log Analyst + Deploy Correlator, go to Comms

    Args:
        triage_agent: Pre-built Triage Agent (severity classification).
        log_analyst_agent: Pre-built Log Analyst Agent (root cause analysis).
        deploy_correlator_agent: Pre-built Deploy Correlator (deploy ranking).
        remediation_agent: Pre-built Remediation Agent (draft + HITL gate).
        comms_agent: Pre-built Comms Agent (Slack summary).
        groq_client: Pre-configured AsyncOpenAI client pointed at Groq's API.
        model_name: Groq model for the orchestrator. Defaults to
            llama-3.3-70b-versatile — routing decisions require solid reasoning
            to follow the workflow correctly and avoid unnecessary tool calls.

    Returns:
        Configured Orchestrator Agent that produces an ``IncidentSummary``
        after coordinating all specialist handoffs.
    """
    llm = OpenAIChatCompletionsModel(model=model_name, openai_client=groq_client)

    return Agent(
        name=ORCHESTRATOR_AGENT_NAME,
        instructions=load_prompt("orchestrator.txt"),
        handoffs=[
            triage_agent,
            log_analyst_agent,
            deploy_correlator_agent,
            remediation_agent,
            comms_agent,
        ],
        output_type=IncidentSummary,
        model=llm,
    )
