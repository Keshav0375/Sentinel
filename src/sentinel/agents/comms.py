"""Comms agent — Slack summary drafting."""

from __future__ import annotations

from agents import Agent, Tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from sentinel.agents.loader import load_prompt
from sentinel.models.comms import SlackSummary
from sentinel.tools.comms_tools import make_comms_tools

COMMS_AGENT_NAME = "comms_agent"


def build_comms_agent(
    *,
    groq_client: AsyncOpenAI,
    model_name: str = "llama-3.1-8b-instant",
) -> Agent[SlackSummary]:
    """Create a configured Comms Agent with injected dependencies.

    The agent receives the full incident context (triage result, log analysis,
    deploy correlation, remediation plan, approval status), calls
    ``draft_slack_summary`` to produce a Slack-formatted message, and returns
    a ``SlackSummary`` with the message text and the list of target channels.

    Args:
        groq_client: Pre-configured AsyncOpenAI client pointed at Groq's API.
        model_name: Groq model to use. Defaults to llama-3.1-8b-instant (the
            fast model, equivalent to gpt-4o-mini — Slack summary drafting is
            a formatting task, not a complex reasoning task).

    Returns:
        Configured Agent that produces a ``SlackSummary`` as structured output.
    """
    llm = OpenAIChatCompletionsModel(model=model_name, openai_client=groq_client)

    # make_comms_tools() returns list[FunctionTool]; annotate as list[Tool]
    # so pyright accepts it as the Agent.tools parameter (list is invariant).
    tools: list[Tool] = [*make_comms_tools()]

    return Agent(
        name=COMMS_AGENT_NAME,
        instructions=load_prompt("comms.txt"),
        tools=tools,
        output_type=SlackSummary,
        model=llm,
    )
