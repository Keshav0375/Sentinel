"""list_recent_deploys tool — deploy history from scenario data (MVP)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agents import FunctionTool, function_tool

from sentinel.generator.deploy_gen import generate_deploys
from sentinel.generator.scenarios import Scenario
from sentinel.models.deploy import Deploy


def make_deploy_checker_tool(scenario: Scenario | None) -> FunctionTool:
    """Create the list_recent_deploys tool with scenario data injected.

    In the MVP, deploy history comes from the pre-loaded scenario rather than
    a real deployment API (GitHub / Argo). The scenario is captured via closure
    so the tool signature stays clean for the LLM.

    Args:
        scenario: Active scenario providing synthetic deploy records. None
            when no scenario is loaded (tool will return an error message).

    Returns:
        FunctionTool that agents can call to retrieve deploy history.
    """

    @function_tool
    async def list_recent_deploys(service_name: str, hours_back: int) -> str:
        """List deploys to a service within the last N hours, newest first.

        Use this to find which deploy is most likely responsible for the
        incident. Rank candidates by timing (proximity to alert), author,
        and scope of changes (files_changed).

        Args:
            service_name: Service to check (e.g. "payment-service").
            hours_back: How far back to look. Start with 2; expand to 4–24
                if no recent deploys are found.
        """
        return await _list_recent_deploys(scenario, service_name, hours_back)

    return list_recent_deploys


async def _list_recent_deploys(
    scenario: Scenario | None,
    service_name: str,
    hours_back: int,
) -> str:
    """List recent deploys for a service from scenario data.

    Extracted from the tool decorator so unit tests can call this directly
    without constructing a ToolContext.

    Args:
        scenario: The loaded scenario supplying synthetic deploy data.
        service_name: Service name to filter on.
        hours_back: Look-back window in hours relative to the incident time.

    Returns:
        Human-readable deploy summary or an error string.
    """
    if scenario is None:
        return "ERROR: No scenario loaded. Cannot list deploys without scenario data."

    if not service_name:
        return "ERROR: 'service_name' parameter is required."

    if hours_back <= 0:
        return "ERROR: 'hours_back' must be a positive integer."

    reference_time = _get_reference_time(scenario)
    window_start = reference_time - timedelta(hours=hours_back)

    all_deploys = generate_deploys(scenario)
    filtered = _filter_deploys(all_deploys, service_name, window_start, reference_time)
    return _format_deploys(filtered, service_name, hours_back, reference_time)


def _get_reference_time(scenario: Scenario) -> datetime:
    """Derive the incident reference time from the scenario.

    Returns the latest timestamp across all log entries and deploy records.
    This approximates when the incident was active, so that hours_back is
    measured from the right point in time rather than from datetime.now().
    """
    candidates: list[datetime] = []

    if scenario.logs:
        candidates.append(max(entry.ts for entry in scenario.logs))

    if scenario.deploys:
        candidates.append(max(d.ts for d in scenario.deploys))

    return max(candidates) if candidates else datetime.now(UTC)


def _filter_deploys(
    deploys: list[Deploy],
    service_name: str,
    window_start: datetime,
    reference_time: datetime,
) -> list[Deploy]:
    """Keep deploys for the named service within the look-back window."""
    result = [d for d in deploys if d.service == service_name]
    result = [d for d in result if window_start <= d.timestamp <= reference_time]
    return result  # generate_deploys already sorts newest-first


def _format_deploys(
    deploys: list[Deploy],
    service_name: str,
    hours_back: int,
    reference_time: datetime,
) -> str:
    """Render the deploy list as a compact, LLM-readable string."""
    if not deploys:
        return (
            f"No deploys found for '{service_name}' "
            f"in the last {hours_back} hour(s) before the incident."
        )

    ref_ts = reference_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        f"Found {len(deploys)} deploy(s) for '{service_name}' "
        f"in the last {hours_back} hour(s) (before {ref_ts}):\n"
    )

    blocks: list[str] = []
    for deploy in deploys:
        ts = deploy.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        blocks.append(
            f"deploy_id:     {deploy.id}\n"
            f"  timestamp:     {ts}\n"
            f"  author:        {deploy.author}\n"
            f"  commit_sha:    {deploy.commit_sha}\n"
            f"  files_changed: {deploy.files_changed}\n"
            f"  description:   {deploy.description or 'none'}"
        )

    return header + "\n\n".join(blocks)
