"""Comms agent — Slack summary drafting."""

from __future__ import annotations

from typing import Any

from agents import Agent, Tool

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.models.comms import SlackSummary
from sentinel.providers import get_capabilities, resolve_model
from sentinel.tools.comms_tools import make_comms_tools

COMMS_AGENT_NAME = "comms_agent"


def build_comms_agent(
    *,
    model_string: str,
    settings: Settings,
) -> Agent[SlackSummary]:
    """Create a configured Comms Agent with injected dependencies.

    The agent receives the full incident context (triage result, log analysis,
    deploy correlation, remediation plan, approval status), calls
    ``draft_slack_summary`` to produce a Slack-formatted message, and returns
    a ``SlackSummary`` with the message text and the list of target channels.

    Args:
        model_string: Provider/model string (e.g. 'groq/llama-3.1-8b-instant').
        settings: Validated Settings instance for API key lookup.

    Returns:
        Configured Agent that produces a ``SlackSummary`` as structured output
        when the provider supports it, or plain text otherwise.
    """
    llm = resolve_model(model_string, settings)
    caps = get_capabilities(model_string)

    # make_comms_tools() returns list[FunctionTool]; annotate as list[Tool]
    # so pyright accepts it as the Agent.tools parameter (list is invariant).
    tools: list[Tool] = [*make_comms_tools()]

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = SlackSummary
    if caps.default_model_settings:
        kwargs["model_settings"] = caps.default_model_settings

    return Agent(
        name=COMMS_AGENT_NAME,
        instructions=load_prompt("comms.txt"),
        tools=tools,
        model=llm,
        **kwargs,
    )
