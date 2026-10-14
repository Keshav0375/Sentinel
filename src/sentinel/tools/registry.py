"""Central tool registry — dependency injection container and tool factory.

Pattern
-------
Each tool is an async function decorated with ``@function_tool``. Tools cannot
receive their dependencies (memory clients, the database pool) as LLM-visible
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
        semantic_memory=SemanticMemory(pool),  # pool: asyncpg.Pool | None
        episodic_memory=EpisodicMemory(pool, embedding_client),
        pool=pool,
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

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from agents import FunctionTool

from sentinel.config import Settings
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
        pool: asyncpg pool ``get_deploy_details`` reads ``deployments`` from. None
            (no ``DATABASE_URL``) builds fine; the tool then reports "unavailable".
        settings: Credentials and endpoints for the API-backed tools (Datadog for
            ``fetch_logs``, GitHub for ``get_deploy_details``). Defaults to the
            environment's Settings; keys are read at call time, so a missing key fails
            (or, for GitHub, skips) the call, never the build.
    """

    semantic_memory: SemanticMemory
    episodic_memory: EpisodicMemory
    pool: asyncpg.Pool | None = field(default=None)
    settings: Settings = field(default_factory=Settings)


def build_tools(deps: ToolDeps) -> list[FunctionTool]:
    """Create all function tools with their dependencies injected via closure.

    Returns a flat list of ``FunctionTool`` instances ready to be passed to
    an ``Agent``: exactly the six Phase-2 tools (decision 2026-10-14) —
    four read-only lookups and two output-only drafts. No tool notifies,
    approves or acts on the world (backend.md §3.3).

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

    tools.append(make_log_fetcher_tool(deps.settings))

    # ── 4.4 — get_deploy_details ─────────────────────────────────────────────
    from sentinel.tools.deploy_checker import make_deploy_checker_tool

    tools.append(make_deploy_checker_tool(deps.pool, deps.settings))

    # ── 4.5 — search_past_incidents ──────────────────────────────────────────
    from sentinel.tools.incident_search import make_incident_search_tool

    tools.append(make_incident_search_tool(deps.episodic_memory))

    # ── 4.6 — prepare_rollback_spec, format_escalation (output only) ─────────
    from sentinel.tools.remediation_tools import make_remediation_tools

    tools.extend(make_remediation_tools())

    return tools
