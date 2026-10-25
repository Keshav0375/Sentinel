"""Reverification agent — the §4.4 PASS/FAIL/ESCALATE check of a rollback spec (no tools).

It answers with one plain-text line that
:func:`~sentinel.models.reverification.parse_reverification` turns into a verdict; anything
malformed escalates. Even a PASS only lets the orchestrator return the spec: GitHub Actions
opens the revert PR and a human merges or closes it.
"""

from __future__ import annotations

from typing import Any, Final

from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.providers import REVERIFICATION_AGENT, Role, model_for

# The agent name is also its prompt file and LangFuse prompt name (decision 2026-10-15).
REVERIFICATION_AGENT_NAME: Final = REVERIFICATION_AGENT
# The §4.7 role whose model pair this agent runs on (providers/router.py).
_ROLE: Role = "reverification"


def build_reverification_agent(
    *, settings: Settings, instructions: str | None = None
) -> Agent[Any]:
    """The ``reverification`` agent: no tools, no handoffs, plain-text output.

    Args:
        settings: Selects the primary provider of the role pair and supplies its key.
        instructions: The system prompt (``infra/tracing.load_prompt``, preamble included);
            None reads ``prompts/reverification.txt``.
    """
    llm, _ = model_for(_ROLE, settings)
    return Agent(
        name=REVERIFICATION_AGENT_NAME,
        instructions=instructions or load_prompt(f"{REVERIFICATION_AGENT_NAME}.txt"),
        tools=[],
        handoffs=[],
        model=llm,
    )
