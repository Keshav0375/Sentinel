"""fetch_logs tool — log retrieval from scenario data (MVP)."""

from __future__ import annotations

from datetime import UTC, datetime

from agents import FunctionTool, function_tool

from sentinel.generator.log_gen import generate_logs
from sentinel.generator.scenarios import Scenario
from sentinel.models.log_entry import LogEntry

# Severity ordering for minimum-level filtering (DEBUG is least severe).
_LEVEL_ORDER: dict[str, int] = {
    "DEBUG": 0,
    "INFO": 1,
    "WARNING": 2,
    "ERROR": 3,
    "CRITICAL": 4,
}


def make_log_fetcher_tool(scenario: Scenario | None) -> FunctionTool:
    """Create the fetch_logs tool with scenario data injected.

    In the MVP, logs come from the pre-loaded scenario rather than a real
    log aggregation API (Datadog / CloudWatch). The scenario is captured via
    closure so the tool signature stays clean for the LLM.

    Args:
        scenario: Active scenario providing synthetic log entries. None when
            no scenario is loaded (tool will return an error message).

    Returns:
        FunctionTool that agents can call to retrieve service logs.
    """

    @function_tool(strict_mode=False)
    async def fetch_logs(
        service: str,
        start_time: str,
        end_time: str,
        level_filter: str = "",
        keyword_filter: str = "",
    ) -> str:
        """Fetch log entries for a service within a time window.

        Returns structured log lines relevant to the incident. Prefer a narrow
        window (30 minutes before the alert through the alert time) for focused
        results. Noise logs from unrelated services are included in the raw
        stream but excluded when you specify a service name.

        Args:
            service: Service to fetch logs for (e.g. "api-gateway").
            start_time: ISO-8601 start of the window (e.g. "2026-05-10T02:30:00Z").
            end_time: ISO-8601 end of the window (e.g. "2026-05-10T03:05:00Z").
            level_filter: Minimum severity to return. One of DEBUG, INFO,
                WARNING, ERROR, CRITICAL. "ERROR" returns ERROR and CRITICAL.
                Leave blank to return all levels.
            keyword_filter: Case-insensitive substring to match against log
                messages. Leave blank for no keyword filtering.
        """
        return await _fetch_logs(
            scenario, service, start_time, end_time, level_filter, keyword_filter
        )

    return fetch_logs


async def _fetch_logs(
    scenario: Scenario | None,
    service: str,
    start_time: str,
    end_time: str,
    level_filter: str = "",
    keyword_filter: str = "",
) -> str:
    """Fetch and filter logs from the active scenario.

    Extracted from the tool decorator for direct testability without needing
    a ToolContext from the Agents SDK.

    Args:
        scenario: The loaded scenario supplying synthetic log data.
        service: Service name to filter on.
        start_time: ISO-8601 window start (Z suffix treated as UTC).
        end_time: ISO-8601 window end.
        level_filter: Minimum severity (e.g. "ERROR" → ERROR + CRITICAL).
        keyword_filter: Case-insensitive message substring filter.

    Returns:
        Human-readable log dump or an error string.
    """
    if scenario is None:
        return "ERROR: No scenario loaded. Cannot fetch logs without scenario data."

    if not service:
        return "ERROR: 'service' parameter is required."

    try:
        start_dt = _parse_iso(start_time)
        end_dt = _parse_iso(end_time)
    except ValueError as exc:
        return f"ERROR: Invalid time format — {exc}. Use ISO-8601, e.g. '2026-05-10T02:30:00Z'."

    if start_dt > end_dt:
        return "ERROR: start_time must be before end_time."

    all_logs = generate_logs(scenario, noise=True)
    filtered = _filter_logs(all_logs, service, start_dt, end_dt, level_filter, keyword_filter)
    return _format_logs(filtered, service, start_time, end_time, len(all_logs))


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp string, treating bare timestamps as UTC."""
    normalized = ts.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _filter_logs(
    logs: list[LogEntry],
    service: str,
    start_dt: datetime,
    end_dt: datetime,
    level_filter: str,
    keyword_filter: str,
) -> list[LogEntry]:
    """Apply service, time-range, level, and keyword filters."""
    result = [e for e in logs if e.service == service]
    result = [e for e in result if start_dt <= e.timestamp <= end_dt]

    if level_filter:
        min_ord = _LEVEL_ORDER.get(level_filter.upper(), 0)
        result = [e for e in result if _LEVEL_ORDER.get(e.level.upper(), 0) >= min_ord]

    if keyword_filter:
        kw = keyword_filter.lower()
        result = [e for e in result if kw in e.message.lower()]

    return result


def _format_logs(
    entries: list[LogEntry],
    service: str,
    start_time: str,
    end_time: str,
    total_raw_count: int,
) -> str:
    """Render filtered log entries as a compact, LLM-readable string."""
    if not entries:
        return f"No log entries found for '{service}' between {start_time} and {end_time}."

    header = f"Found {len(entries)} log entries for '{service}' ({start_time} → {end_time}):\n"
    lines: list[str] = []
    for entry in entries:
        ts = entry.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        level = entry.level.upper().ljust(8)
        lines.append(f"[{ts}] {level} {entry.message}")

    footer = f"\n({len(entries)} of {total_raw_count} total logs shown after filtering)"
    return header + "\n".join(lines) + footer
