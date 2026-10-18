"""Reflexion agent — the §4.3 critique of an Analysis hypothesis (no tools).

A step of the orchestrator's loop, not a free agent: it reads the hypothesis and its
evidence and answers with one :class:`~sentinel.models.reflexion.ReflexionResult` as JSON.
The orchestrator decides what to do with it (loop back to Analysis, or go to the gate).
"""

from __future__ import annotations

from typing import Any, Final

from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.models.reflexion import ReflexionResult
from sentinel.providers import REFLEXION_AGENT, Role, model_for, role_capabilities

# The agent name is also its prompt file and LangFuse prompt name (decision 2026-10-15).
REFLEXION_AGENT_NAME: Final = REFLEXION_AGENT
# The §4.7 role whose model pair this agent runs on (providers/router.py).
_ROLE: Role = "reflexion"


def build_reflexion_agent(*, settings: Settings, instructions: str | None = None) -> Agent[Any]:
    """The ``reflexion`` agent: no tools, no handoffs.

    Args:
        settings: Selects the primary provider of the role pair and supplies its key.
        instructions: The system prompt (``infra/tracing.load_prompt``, preamble included);
            None reads ``prompts/reflexion.txt``.

    Returns:
        An agent producing a ``ReflexionResult`` as structured output when the provider
        supports it, or its JSON as plain text otherwise.
    """
    llm, _ = model_for(_ROLE, settings)
    caps = role_capabilities(_ROLE, settings)

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = ReflexionResult

    return Agent(
        name=REFLEXION_AGENT_NAME,
        instructions=instructions or load_prompt(f"{REFLEXION_AGENT_NAME}.txt"),
        tools=[],
        handoffs=[],
        model=llm,
        **kwargs,
    )
