"""get_service_metadata tool — service map lookup via semantic memory."""

from __future__ import annotations

from agents import FunctionTool, function_tool

from sentinel.memory.semantic import SemanticMemory
from sentinel.models.memory import SemanticRecord


def make_service_lookup_tool(memory: SemanticMemory) -> FunctionTool:
    """Create the get_service_metadata tool with semantic memory injected.

    Args:
        memory: SemanticMemory instance for service and runbook lookups.

    Returns:
        FunctionTool that agents can call to look up service metadata.
    """

    @function_tool(strict_mode=False)
    async def get_service_metadata(service_name: str) -> str:
        """Get ownership, tier, on-call channel, dependencies, and runbook steps for a service.

        Use this before any analysis to understand the service topology, who
        owns the affected service, and what remediation runbooks exist for
        common failure classes on that service.

        Args:
            service_name: Exact service name (e.g. "payment-service").
        """
        return await _build_service_response(memory, service_name)

    return get_service_metadata


async def _build_service_response(memory: SemanticMemory, service_name: str) -> str:
    """Build the formatted service metadata string returned to the LLM.

    Separated from the tool decorator so unit tests can call this directly
    without constructing a ToolContext.

    Args:
        memory: SemanticMemory to query.
        service_name: The service to look up.

    Returns:
        Human-readable service summary, or an error string if not found.
    """
    try:
        record = await memory.query(service_name)
    except KeyError:
        return f"ERROR: Service '{service_name}' not found in the service map."

    return _format_record(record)


def _format_record(record: SemanticRecord) -> str:
    """Format a SemanticRecord into a compact, LLM-readable string."""
    svc = record.service
    deps = ", ".join(svc.dependencies) if svc.dependencies else "none"

    lines: list[str] = [
        f"name: {svc.name}",
        f"team: {svc.team}",
        f"tier: {svc.tier}",
        f"oncall_channel: {svc.oncall_channel or 'none'}",
        f"repo_url: {svc.repo_url or 'unknown'}",
        f"description: {svc.description or 'none'}",
        f"dependencies: {deps}",
    ]

    if record.runbooks:
        lines.append(f"runbooks ({len(record.runbooks)}):")
        for rb in record.runbooks:
            # Show up to the first 3 steps as a preview so the LLM knows
            # what remediation paths are available for each failure class.
            preview_steps = rb.steps[:3]
            step_str = " → ".join(preview_steps)
            if len(rb.steps) > 3:
                step_str += f" ... ({len(rb.steps)} steps total)"
            lines.append(f"  [{rb.failure_class}] {step_str}")
    else:
        lines.append("runbooks: none")

    return "\n".join(lines)
