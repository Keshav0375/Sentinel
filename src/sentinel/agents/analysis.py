"""Analysis agent — Datadog evidence + deploy history → one root-cause hypothesis (§4.2).

Merges the Phase-1 log analyst and deploy correlator (decision 2026-10-15): one agent reads
``fetch_logs`` and ``get_deploy_details`` and answers with an :class:`AnalysisResult`. When
the ``reflexion`` critique sends it back, the orchestrator puts the critique's next tool
call in its input. Both tools are read-only.
"""

from __future__ import annotations

from typing import Any, Final

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from agents import Agent

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.models.analysis import AnalysisResult
from sentinel.providers import Role, model_for, role_capabilities
from sentinel.tools.deploy_checker import make_deploy_checker_tool
from sentinel.tools.log_fetcher import make_log_fetcher_tool

# The agent name is also its prompt file and LangFuse prompt name (decision 2026-10-15).
ANALYSIS_AGENT_NAME: Final = "analysis"
# The §4.7 role whose model pair this agent runs on (providers/router.py).
_ROLE: Role = "analysis"


def build_analysis_agent(
    pool: asyncpg.Pool | None,
    *,
    settings: Settings,
    instructions: str | None = None,
) -> Agent[Any]:
    """Create a configured Analysis Agent with injected dependencies.

    Args:
        pool: asyncpg pool holding ``deployments``. ``None`` (no ``DATABASE_URL``) makes
            ``get_deploy_details`` return a structured "unavailable" failure at call time
            rather than crashing at construction.
        settings: Selects the primary provider and supplies its key; also the Datadog keys
            ``fetch_logs`` and the GitHub token ``get_deploy_details`` read at call time.
        instructions: The system prompt (``infra/tracing.load_prompt``, preamble included);
            None reads ``prompts/analysis.txt``.

    Returns:
        Configured Agent with exactly ``fetch_logs`` and ``get_deploy_details``, producing an
        ``AnalysisResult`` as structured output when the provider supports it, or its JSON
        as plain text otherwise.
    """
    llm, _ = model_for(_ROLE, settings)
    caps = role_capabilities(_ROLE, settings)

    kwargs: dict[str, Any] = {}
    if caps.supports_structured_outputs:
        kwargs["output_type"] = AnalysisResult

    return Agent(
        name=ANALYSIS_AGENT_NAME,
        instructions=instructions or load_prompt(f"{ANALYSIS_AGENT_NAME}.txt"),
        tools=[
            make_log_fetcher_tool(settings),
            make_deploy_checker_tool(pool, settings),
        ],
        model=llm,
        **kwargs,
    )
