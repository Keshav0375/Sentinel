"""get_service_metadata tool — service map lookup via semantic memory (§5.3 ``services``)."""

from __future__ import annotations

from agents import FunctionTool, function_tool

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.logging import get_logger
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.memory import SemanticRecord
from sentinel.models.tools import ServiceLookupInput, ToolFailure

logger = get_logger("sentinel.tools.service_lookup")

_TOOL_NAME = "get_service_metadata"


def make_service_lookup_tool(memory: SemanticMemory) -> FunctionTool:
    """Create the get_service_metadata tool with semantic memory injected.

    Args:
        memory: SemanticMemory instance for service and runbook lookups.

    Returns:
        FunctionTool that agents can call to look up service metadata.
    """

    @function_tool(strict_mode=False)
    async def get_service_metadata(request: ServiceLookupInput) -> SemanticRecord | ToolFailure:
        """Get ownership, tier, on-call channel, dependencies, and runbook steps for a service.

        Use this before any analysis to understand the service topology, who
        owns the affected service, and what remediation runbook exists for it.
        Returns a structured failure (kind "not_found") for a service that is not
        in the service map, and (kind "unavailable") when memory cannot be read —
        the latter is no finding about the service either way.

        Args:
            request: The service to look up, by exact name.
        """
        return await _lookup_service(memory, request)

    return get_service_metadata


async def _lookup_service(
    memory: SemanticMemory, request: ServiceLookupInput
) -> SemanticRecord | ToolFailure:
    """Return the service's metadata and runbook, or a ToolFailure — never raise.

    Separated from the tool decorator so tests can call it without a ToolContext.
    """
    name = request.service_name.strip()
    if not name:
        return ToolFailure(tool=_TOOL_NAME, kind="bad_input", message="service_name is empty")

    try:
        return await memory.query(name)
    except KeyError:
        return ToolFailure(
            tool=_TOOL_NAME,
            kind="not_found",
            message=f"service {name!r} is not in the service map",
        )
    except SentinelMemoryError as exc:
        # No database configured, or it failed: say so plainly — "not found" here would
        # read as "this service does not exist", which is a different (false) finding.
        logger.warning("service_lookup_memory_unavailable", error=str(exc))
        return ToolFailure(
            tool=_TOOL_NAME,
            kind="unavailable",
            message=f"service metadata cannot be looked up: {exc}",
        )
