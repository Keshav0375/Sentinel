"""Remediation agent — rollback/hotfix drafting with HITL gate."""

from __future__ import annotations

from typing import Any

from agents import Agent, Tool

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.models.remediation import RemediationPlan
from sentinel.providers import get_capabilities, resolve_model
from sentinel.tools.hitl import ApprovalFn, make_hitl_tool
from sentinel.tools.remediation_tools import make_remediation_tools

REMEDIATION_AGENT_NAME = "remediation_agent"


def build_remediation_agent(
    *,
    model_string: str,
    settings: Settings,
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
        model_string: Provider/model string (e.g. 'groq/llama-3.3-70b-versatile').
        settings: Validated Settings instance for API key lookup.
        approval_fn: Optional HITL callback. Defaults to terminal ``input()``
            in the MVP. Inject a mock or async webhook handler in tests or
            Phase 2 (Slack interactive buttons).

    Returns:
        Configured Agent that produces a ``RemediationPlan`` as structured
        output after obtaining human approval.
    """
    llm = resolve_model(model_string, settings)
    caps = get_capabilities(model_string)

    # make_remediation_tools() returns [draft_rollback_pr, draft_hotfix]
    tools: list[Tool] = [
        *make_remediation_tools(),
        make_hitl_tool(approval_fn=approval_fn),
    ]

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = RemediationPlan
    if caps.default_model_settings:
        kwargs["model_settings"] = caps.default_model_settings

    return Agent(
        name=REMEDIATION_AGENT_NAME,
        instructions=load_prompt("remediation.txt"),
        tools=tools,
        model=llm,
        **kwargs,
    )
