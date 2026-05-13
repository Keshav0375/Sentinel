"""Remediation agent — rollback/hotfix drafting with HITL gate."""

from __future__ import annotations

from agents import Agent, Tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from sentinel.agents.loader import load_prompt
from sentinel.models.remediation import RemediationPlan
from sentinel.tools.hitl import ApprovalFn, make_hitl_tool
from sentinel.tools.remediation_tools import make_remediation_tools

REMEDIATION_AGENT_NAME = "remediation_agent"


def build_remediation_agent(
    *,
    groq_client: AsyncOpenAI,
    model_name: str = "llama-3.3-70b-versatile",
    approval_fn: ApprovalFn | None = None,
) -> Agent[RemediationPlan]:
    """Create a configured Remediation Agent with HITL gate.

    The agent receives root cause findings (hypothesis, suspect deploy, log
    evidence) from the orchestrator, decides between rollback and hotfix,
    drafts the remediation artifact via the appropriate draft tool, and
    ALWAYS calls ``request_human_approval`` before returning its plan.

    The HITL gate is non-negotiable: the agent's system prompt explicitly
    mandates it, the only path to a remediation plan is through the approval
    gate, and there is no "execute" tool — only "draft" + "request approval."

    Args:
        groq_client: Pre-configured AsyncOpenAI client pointed at Groq's API.
        model_name: Groq model to use. Defaults to llama-3.3-70b-versatile
            (the capable reasoning model, equivalent to gpt-4o — remediation
            decisions require careful evidence evaluation and risk assessment).
        approval_fn: Optional HITL callback. Defaults to terminal ``input()``
            in the MVP. Inject a mock or async webhook handler in tests or
            Phase 2 (Slack interactive buttons).

    Returns:
        Configured Agent that produces a ``RemediationPlan`` as structured
        output after obtaining human approval.
    """
    llm = OpenAIChatCompletionsModel(model=model_name, openai_client=groq_client)

    # make_remediation_tools() returns [draft_rollback_pr, draft_hotfix]
    tools: list[Tool] = [
        *make_remediation_tools(),
        make_hitl_tool(approval_fn=approval_fn),
    ]

    return Agent(
        name=REMEDIATION_AGENT_NAME,
        instructions=load_prompt("remediation.txt"),
        tools=tools,
        output_type=RemediationPlan,
        model=llm,
    )
