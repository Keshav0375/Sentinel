"""draft_slack_summary tool — incident communication formatter."""

from __future__ import annotations

import json
from typing import cast

from agents import FunctionTool, function_tool


def make_comms_tools() -> list[FunctionTool]:
    """Create the communication tools (currently: draft_slack_summary).

    Returns:
        List containing [draft_slack_summary] FunctionTool.
    """

    @function_tool(strict_mode=False)
    async def draft_slack_summary(incident_summary: str) -> str:
        """Draft a structured Slack incident summary for the #incidents channel.

        Formats the incident state into the standard template:
        Impact / Root Cause / Timeline / Current Status / Action Items / ETA.

        Args:
            incident_summary: JSON or structured text describing the full
                incident state. Preferred JSON keys: service, severity,
                impact, root_cause, timeline, status, action_items, eta.
                Falls back to a generic template if raw text is provided.
        """
        return _draft_slack_summary(incident_summary)

    return [draft_slack_summary]


_TEMPLATE_KEYS = (
    "service",
    "severity",
    "impact",
    "root_cause",
    "timeline",
    "status",
    "action_items",
    "eta",
)


def _draft_slack_summary(incident_summary: str) -> str:
    """Generate a Slack-formatted incident summary.

    Extracted from the tool decorator so unit tests can call this directly
    without constructing a ToolContext.
    """
    if not incident_summary.strip():
        return "ERROR: 'incident_summary' must be a non-empty string."

    data = _parse_incident_data(incident_summary)
    return _format_slack_message(data)


def _parse_incident_data(incident_summary: str) -> dict[str, str]:
    """Try JSON parsing, fall back to raw text wrapped in template keys."""
    try:
        raw = json.loads(incident_summary)
        if isinstance(raw, dict):
            parsed = cast(dict[str, object], raw)
            return {k: str(parsed.get(k, "N/A")) for k in _TEMPLATE_KEYS}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    return {
        "service": "N/A",
        "severity": "N/A",
        "impact": "See summary below.",
        "root_cause": "See summary below.",
        "timeline": incident_summary.strip(),
        "status": "N/A",
        "action_items": "N/A",
        "eta": "N/A",
    }


def _format_slack_message(data: dict[str, str]) -> str:
    """Apply the standard Slack incident template."""
    return (
        f":rotating_light: *Incident Summary* — "
        f"{data['service']} ({data['severity']})\n"
        f"\n"
        f"*Impact*\n"
        f"{data['impact']}\n"
        f"\n"
        f"*Root Cause*\n"
        f"{data['root_cause']}\n"
        f"\n"
        f"*Timeline*\n"
        f"{data['timeline']}\n"
        f"\n"
        f"*Current Status*\n"
        f"{data['status']}\n"
        f"\n"
        f"*Action Items*\n"
        f"{data['action_items']}\n"
        f"\n"
        f"*ETA to Resolution*\n"
        f"{data['eta']}"
    )
