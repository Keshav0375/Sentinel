"""Central tool registry — dependency injection container and tool factory.

Pattern
-------
Each tool is an async function decorated with ``@function_tool``. Tools cannot
receive their dependencies (memory clients, scenario data) as LLM-visible
parameters — the LLM only sees the typed domain input schema. Dependencies
are therefore injected at construction time via closure::

    def make_get_service_metadata(memory: SemanticMemory) -> FunctionTool:
        @function_tool
        async def get_service_metadata(service_name: str) -> ServiceMetadata:
            \"\"\"Get metadata for a named service from the service map.\"\"\"
            return await memory.get_service(service_name)
        return get_service_metadata

The registry wires all tools together in ``build_tools``. Agents receive a
pre-built list of ``FunctionTool`` objects — they never import tool modules
directly. This keeps agent definitions decoupled from tool implementations.

Usage::

    deps = ToolDeps(
        semantic_memory=SemanticMemory(db_path),
        episodic_memory=EpisodicMemory(db_path, embedding_client),
        scenario=loaded_scenario,
    )
    tools = build_tools(deps)
    agent = Agent(name="triage", tools=tools, ...)

Adding a new tool
-----------------
1. Implement ``make_<tool>_tool(...)`` in ``sentinel/tools/<module>.py``.
2. Add an import + ``tools.append(...)`` block inside ``build_tools`` below,
   following the existing pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agents import FunctionTool

from sentinel.generator.scenarios import Scenario
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory


@dataclass
class ToolDeps:
    """Bundle of injectable dependencies for all Sentinel tools.

    Passed to ``build_tools`` so each tool factory can capture what it needs
    via closure. Never imported as a singleton — always constructed and
    injected by the caller (FastAPI lifespan handler or test fixture).

    Attributes:
        semantic_memory: Service map and runbook lookups (SemanticMemory).
        episodic_memory: Past-incident similarity search (EpisodicMemory).
        scenario: Active scenario supplying synthetic logs and deploys in the
            MVP. None when no scenario is pre-loaded (e.g. registry-only unit
            tests that don't exercise log/deploy tools).
    """

    semantic_memory: SemanticMemory
    episodic_memory: EpisodicMemory
    scenario: Scenario | None = field(default=None)


def build_tools(deps: ToolDeps) -> list[FunctionTool]:
    """Create all function tools with their dependencies injected via closure.

    Returns a flat list of ``FunctionTool`` instances ready to be passed to
    an ``Agent``. The list grows as tasks 4.2–4.8 are implemented — each
    adds an import block and a ``tools.append`` call below.

    Args:
        deps: The bundled dependencies for this request context.

    Returns:
        List of FunctionTool instances in the order they were registered.
    """
    tools: list[FunctionTool] = []

    # ── 4.2 — get_service_metadata ───────────────────────────────────────────
    from sentinel.tools.service_lookup import make_service_lookup_tool

    tools.append(make_service_lookup_tool(deps.semantic_memory))

    # ── 4.3 — fetch_logs ─────────────────────────────────────────────────────
    from sentinel.tools.log_fetcher import make_log_fetcher_tool

    tools.append(make_log_fetcher_tool(deps.scenario))

    # ── 4.4 — list_recent_deploys ─────────────────────────────────────────────
    from sentinel.tools.deploy_checker import make_deploy_checker_tool

    tools.append(make_deploy_checker_tool(deps.scenario))

    # ── 4.5 — search_past_incidents ──────────────────────────────────────────
    from sentinel.tools.incident_search import make_incident_search_tool

    tools.append(make_incident_search_tool(deps.episodic_memory))

    # ── 4.6 — draft_rollback_pr, draft_hotfix ────────────────────────────────
    from sentinel.tools.remediation_tools import make_remediation_tools

    tools.extend(make_remediation_tools())

    # ── 4.7 — draft_slack_summary ────────────────────────────────────────────
    from sentinel.tools.comms_tools import make_comms_tools

    tools.extend(make_comms_tools())

    # ── 4.8 — request_human_approval (HITL gate) ─────────────────────────────
    from sentinel.tools.hitl import make_hitl_tool

    tools.append(make_hitl_tool())

    return tools
