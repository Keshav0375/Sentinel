"""request_human_approval HITL gate — the critical safety boundary.

No agent can perform a destructive or irreversible action without passing
through this gate. There is no "execute" tool — only draft tools +
this approval gate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from agents import FunctionTool, function_tool

from sentinel.infra.logging import get_logger

logger = get_logger("sentinel.tools.hitl")

ApprovalFn = Callable[[str], Awaitable[tuple[str, str | None]]]


def make_hitl_tool(
    approval_fn: ApprovalFn | None = None,
) -> FunctionTool:
    """Create the request_human_approval HITL gate tool.

    Args:
        approval_fn: Async callback that receives the formatted approval
            request and returns ``(decision, comment)``. Decision must be
            ``"approve"`` or ``"reject"``. Defaults to terminal ``input()``
            in the MVP; swap for Slack/API in Phase 2.

    Returns:
        FunctionTool that blocks on human approval before returning.
    """
    handler = approval_fn or _terminal_approval

    @function_tool(strict_mode=False)
    async def request_human_approval(
        action: str,
        risk_level: str,
        evidence_summary: str,
        proposed_by: str,
    ) -> str:
        """Request human approval before executing a potentially destructive action.

        ALWAYS call this before any rollback, hotfix merge, service restart,
        or other action that modifies external state. Never bypass this gate.

        Args:
            action: Human-readable description of the proposed action
                (e.g. "Roll back deploy-abc123 on payment-service").
            risk_level: Estimated risk — one of "high", "medium", or "low".
            evidence_summary: Why this action is recommended; what evidence
                supports it. Be specific.
            proposed_by: Name of the agent proposing the action
                (e.g. "remediation_agent").
        """
        return await _request_human_approval(
            action, risk_level, evidence_summary, proposed_by, handler
        )

    return request_human_approval


async def _request_human_approval(
    action: str,
    risk_level: str,
    evidence_summary: str,
    proposed_by: str,
    approval_fn: ApprovalFn,
) -> str:
    """Core HITL logic, extracted for testability.

    Validates inputs, formats the request, calls the approval handler,
    logs the decision, and returns a formatted result string.
    """
    if not action.strip():
        return "ERROR: 'action' must be a non-empty string."

    if not risk_level.strip():
        return "ERROR: 'risk_level' must be a non-empty string."

    if not evidence_summary.strip():
        return "ERROR: 'evidence_summary' must be a non-empty string."

    if not proposed_by.strip():
        return "ERROR: 'proposed_by' must be a non-empty string."

    display = _format_request_display(action, risk_level, evidence_summary, proposed_by)

    decision, comment = await approval_fn(display)
    status = "APPROVED" if decision.strip().lower() == "approve" else "REJECTED"

    logger.info(
        "hitl_decision",
        action=action,
        risk_level=risk_level,
        proposed_by=proposed_by,
        status=status,
        comment=comment,
    )

    return _format_result(action, risk_level, proposed_by, status, comment)


def _format_request_display(
    action: str,
    risk_level: str,
    evidence_summary: str,
    proposed_by: str,
) -> str:
    """Format the approval request for display to the human reviewer."""
    risk_indicator = "!!!" if risk_level.strip().lower() == "high" else "!"
    return (
        f"\n{'=' * 60}\n"
        f"{risk_indicator} HITL APPROVAL REQUEST {risk_indicator}\n"
        f"{'=' * 60}\n"
        f"\n"
        f"Proposed by:  {proposed_by}\n"
        f"Risk level:   {risk_level}\n"
        f"\n"
        f"ACTION: {action}\n"
        f"\n"
        f"EVIDENCE:\n"
        f"{evidence_summary}\n"
        f"\n"
        f"{'=' * 60}"
    )


def _format_result(
    action: str,
    risk_level: str,
    proposed_by: str,
    status: str,
    comment: str | None,
) -> str:
    """Format the approval result for the agent."""
    comment_line = f"comment:       {comment}" if comment else "comment:       none"
    return (
        f"HITL Decision: {status}\n\n"
        f"action:        {action}\n"
        f"risk_level:    {risk_level}\n"
        f"proposed_by:   {proposed_by}\n"
        f"status:        {status}\n"
        f"{comment_line}"
    )


async def _terminal_approval(display: str) -> tuple[str, str | None]:
    """Default MVP approval handler: print request, wait for terminal input."""
    import sys  # noqa: PLC0415

    sys.stdout.buffer.write(display.encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    response = await asyncio.to_thread(input, "\n>>> Enter 'approve' or 'reject': ")
    decision = response.strip().lower()

    comment: str | None = None
    if decision == "reject":
        raw = await asyncio.to_thread(input, ">>> Reason for rejection (optional): ")
        comment = raw.strip() or None

    return decision, comment
