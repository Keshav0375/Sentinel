"""Resolution agent — drafts a rollback spec or an escalation; it executes nothing.

The renamed Phase-1 remediation agent (decision 2026-10-15). The orchestrator calls it only
past the §4.2 decision gate and reads its draft from the tool output, not from its reply:
``prepare_rollback_spec`` → :class:`~sentinel.models.tools.RollbackSpec`,
``format_escalation`` → :class:`~sentinel.models.tools.EscalationContext`. Both tools are
output-only.

HITL lives outside the backend (backend.md §3.3): GitHub Actions turns the rollback spec
into a revert PR on Sentinel-deployment, and a human merges or closes it. The agent has no
tool that opens, merges or approves anything.
"""

from __future__ import annotations

from typing import Any, Final

from agents import Agent, Tool

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.providers import Role, model_for
from sentinel.tools.remediation_tools import make_remediation_tools

# The agent name is also its prompt file and LangFuse prompt name (decision 2026-10-15).
RESOLUTION_AGENT_NAME: Final = "resolution"
# The §4.7 role whose model pair this agent runs on (providers/router.py).
_ROLE: Role = "resolution"


def build_resolution_agent(*, settings: Settings, instructions: str | None = None) -> Agent[Any]:
    """Create a configured Resolution Agent.

    Args:
        settings: Selects the primary provider of the role pair and supplies its key.
        instructions: The system prompt (``infra/tracing.load_prompt``, preamble included);
            None reads ``prompts/resolution.txt``.

    Returns:
        Agent with exactly ``prepare_rollback_spec`` and ``format_escalation``; its reply
        is plain text (the draft is the tool output).
    """
    llm, _ = model_for(_ROLE, settings)
    # make_remediation_tools() returns [prepare_rollback_spec, format_escalation]
    tools: list[Tool] = [*make_remediation_tools()]
    return Agent(
        name=RESOLUTION_AGENT_NAME,
        instructions=instructions or load_prompt(f"{RESOLUTION_AGENT_NAME}.txt"),
        tools=tools,
        handoffs=[],
        model=llm,
    )
