"""Tests for sentinel.tools.log_fetcher — fetch_logs tool."""

from __future__ import annotations

import pytest
from agents import FunctionTool

from sentinel.generator.alert_gen import load_scenario
from sentinel.generator.log_gen import generate_logs
from sentinel.generator.scenarios import Scenario
from sentinel.tools.log_fetcher import (
    _fetch_logs,
    _filter_logs,
    _format_logs,
    _parse_iso,
    make_log_fetcher_tool,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

# bad_deploy_01: api-gateway, logs span 2026-05-10T02:44:55Z – 03:00:01Z
WIDE_START = "2026-05-10T02:00:00Z"
WIDE_END = "2026-05-10T04:00:00Z"
BEFORE_START = "2026-05-10T01:00:00Z"
BEFORE_END = "2026-05-10T02:30:00Z"  # ends before first log


@pytest.fixture()
def scenario() -> Scenario:
    return load_scenario("bad_deploy_01")


# ── make_log_fetcher_tool ─────────────────────────────────────────────────────


def test_make_returns_function_tool(scenario: Scenario) -> None:
    tool = make_log_fetcher_tool(scenario)
    assert isinstance(tool, FunctionTool)


def test_tool_name_is_fetch_logs(scenario: Scenario) -> None:
    tool = make_log_fetcher_tool(scenario)
    assert tool.name == "fetch_logs"


def test_tool_has_description(scenario: Scenario) -> None:
    tool = make_log_fetcher_tool(scenario)
    assert tool.description and len(tool.description) > 10


def test_tool_schema_has_service(scenario: Scenario) -> None:
    tool = make_log_fetcher_tool(scenario)
    assert "service" in str(tool.params_json_schema)


def test_tool_schema_has_start_time(scenario: Scenario) -> None:
    tool = make_log_fetcher_tool(scenario)
    assert "start_time" in str(tool.params_json_schema)


def test_tool_schema_has_end_time(scenario: Scenario) -> None:
    tool = make_log_fetcher_tool(scenario)
    assert "end_time" in str(tool.params_json_schema)


def test_tool_scenario_not_in_schema(scenario: Scenario) -> None:
    """Injected scenario dep must NOT appear in the LLM-visible schema."""
    tool = make_log_fetcher_tool(scenario)
    assert "scenario" not in str(tool.params_json_schema)


# ── _fetch_logs — None scenario ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_none_scenario_returns_error() -> None:
    result = await _fetch_logs(None, "api-gateway", WIDE_START, WIDE_END)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_none_scenario_does_not_raise() -> None:
    result = await _fetch_logs(None, "api-gateway", WIDE_START, WIDE_END)
    assert isinstance(result, str)


# ── _fetch_logs — parameter validation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_service_returns_error(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "", WIDE_START, WIDE_END)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_invalid_start_time_returns_error(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "api-gateway", "not-a-date", WIDE_END)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_invalid_end_time_returns_error(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "api-gateway", WIDE_START, "also-bad")
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_start_after_end_returns_error(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "api-gateway", WIDE_END, WIDE_START)
    assert "ERROR" in result


# ── _fetch_logs — successful retrieval ────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_string(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "api-gateway", WIDE_START, WIDE_END)
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_returns_logs_for_correct_service(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "api-gateway", WIDE_START, WIDE_END)
    assert "api-gateway" in result


@pytest.mark.asyncio
async def test_log_count_correct(scenario: Scenario) -> None:
    # bad_deploy_01 has 12 api-gateway logs in the scenario window with noise
    result = await _fetch_logs(scenario, "api-gateway", WIDE_START, WIDE_END)
    assert "Found 12" in result


@pytest.mark.asyncio
async def test_result_contains_timestamps(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "api-gateway", WIDE_START, WIDE_END)
    assert "2026-05-10" in result


@pytest.mark.asyncio
async def test_result_contains_log_levels(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "api-gateway", WIDE_START, WIDE_END)
    assert "ERROR" in result or "WARNING" in result


@pytest.mark.asyncio
async def test_known_error_message_present(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "api-gateway", WIDE_START, WIDE_END)
    assert "NullPointerException" in result


@pytest.mark.asyncio
async def test_unknown_service_returns_no_logs_message(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "nonexistent-svc", WIDE_START, WIDE_END)
    assert "No log entries found" in result


@pytest.mark.asyncio
async def test_window_before_all_logs_returns_no_logs(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "api-gateway", BEFORE_START, BEFORE_END)
    assert "No log entries found" in result


@pytest.mark.asyncio
async def test_noise_services_excluded_when_filtering_by_service(scenario: Scenario) -> None:
    # Noise comes from cdn-proxy, notification-service, user-service
    # When we filter by api-gateway, noise should be absent
    result = await _fetch_logs(scenario, "api-gateway", WIDE_START, WIDE_END)
    assert "cdn-proxy" not in result
    assert "Scheduled health check" not in result


# ── _fetch_logs — level filter ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_level_filter_error_only(scenario: Scenario) -> None:
    result = await _fetch_logs(scenario, "api-gateway", WIDE_START, WIDE_END, level_filter="ERROR")
    # Should not have INFO-level entries
    assert "INFO" not in result
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_level_filter_is_minimum_severity(scenario: Scenario) -> None:
    # "ERROR" minimum should include ERROR and CRITICAL lines
    result = await _fetch_logs(scenario, "api-gateway", WIDE_START, WIDE_END, level_filter="ERROR")
    # At least ERROR lines should appear (CRITICAL is ≥ ERROR)
    assert "ERROR" in result or "CRITICAL" in result


@pytest.mark.asyncio
async def test_level_filter_warning_includes_errors(scenario: Scenario) -> None:
    result_warn = await _fetch_logs(
        scenario, "api-gateway", WIDE_START, WIDE_END, level_filter="WARNING"
    )
    result_error = await _fetch_logs(
        scenario, "api-gateway", WIDE_START, WIDE_END, level_filter="ERROR"
    )
    # WARNING filter should return at least as many lines as ERROR filter
    lines_warn = [ln for ln in result_warn.splitlines() if ln.startswith("[")]
    lines_error = [ln for ln in result_error.splitlines() if ln.startswith("[")]
    assert len(lines_warn) >= len(lines_error)


@pytest.mark.asyncio
async def test_level_filter_case_insensitive(scenario: Scenario) -> None:
    result_upper = await _fetch_logs(
        scenario, "api-gateway", WIDE_START, WIDE_END, level_filter="ERROR"
    )
    result_lower = await _fetch_logs(
        scenario, "api-gateway", WIDE_START, WIDE_END, level_filter="error"
    )
    assert result_upper == result_lower


# ── _fetch_logs — keyword filter ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_keyword_filter_matches(scenario: Scenario) -> None:
    result = await _fetch_logs(
        scenario, "api-gateway", WIDE_START, WIDE_END, keyword_filter="NullPointerException"
    )
    assert "NullPointerException" in result


@pytest.mark.asyncio
async def test_keyword_filter_case_insensitive(scenario: Scenario) -> None:
    result_upper = await _fetch_logs(
        scenario, "api-gateway", WIDE_START, WIDE_END, keyword_filter="NULLPOINTEREXCEPTION"
    )
    result_lower = await _fetch_logs(
        scenario, "api-gateway", WIDE_START, WIDE_END, keyword_filter="nullpointerexception"
    )
    assert result_upper == result_lower


@pytest.mark.asyncio
async def test_keyword_filter_non_matching_returns_no_logs(scenario: Scenario) -> None:
    result = await _fetch_logs(
        scenario, "api-gateway", WIDE_START, WIDE_END, keyword_filter="xyzzy_no_such_keyword"
    )
    assert "No log entries found" in result


@pytest.mark.asyncio
async def test_combined_level_and_keyword_filter(scenario: Scenario) -> None:
    result = await _fetch_logs(
        scenario,
        "api-gateway",
        WIDE_START,
        WIDE_END,
        level_filter="ERROR",
        keyword_filter="NullPointerException",
    )
    assert "NullPointerException" in result
    assert "INFO" not in result


# ── _parse_iso ────────────────────────────────────────────────────────────────


def test_parse_iso_z_suffix() -> None:
    dt = _parse_iso("2026-05-10T03:00:01Z")
    assert dt.tzinfo is not None
    assert dt.year == 2026
    assert dt.hour == 3


def test_parse_iso_plus_offset() -> None:
    dt = _parse_iso("2026-05-10T03:00:01+00:00")
    assert dt.tzinfo is not None


def test_parse_iso_naive_gets_utc() -> None:
    dt = _parse_iso("2026-05-10T03:00:01")
    assert dt.tzinfo is not None


def test_parse_iso_invalid_raises() -> None:
    with pytest.raises(ValueError):
        _parse_iso("not-a-date")


# ── _filter_logs (unit tests with real log entries) ───────────────────────────


def test_filter_by_service(scenario: Scenario) -> None:
    all_logs = generate_logs(scenario, noise=True)
    start = _parse_iso(WIDE_START)
    end = _parse_iso(WIDE_END)
    result = _filter_logs(all_logs, "api-gateway", start, end, "", "")
    assert all(e.service == "api-gateway" for e in result)


def test_filter_excludes_out_of_window(scenario: Scenario) -> None:
    all_logs = generate_logs(scenario, noise=False)
    # Narrow window: only first second
    start = _parse_iso("2026-05-10T02:44:55Z")
    end = _parse_iso("2026-05-10T02:44:56Z")
    result = _filter_logs(all_logs, "api-gateway", start, end, "", "")
    assert all(start <= e.timestamp <= end for e in result)


def test_filter_level_exact_match_returns_subset(scenario: Scenario) -> None:
    all_logs = generate_logs(scenario, noise=False)
    start = _parse_iso(WIDE_START)
    end = _parse_iso(WIDE_END)
    all_gw = _filter_logs(all_logs, "api-gateway", start, end, "", "")
    error_gw = _filter_logs(all_logs, "api-gateway", start, end, "ERROR", "")
    assert len(error_gw) <= len(all_gw)


def test_filter_keyword_reduces_results(scenario: Scenario) -> None:
    all_logs = generate_logs(scenario, noise=False)
    start = _parse_iso(WIDE_START)
    end = _parse_iso(WIDE_END)
    all_gw = _filter_logs(all_logs, "api-gateway", start, end, "", "")
    kw_gw = _filter_logs(all_logs, "api-gateway", start, end, "", "NullPointer")
    assert len(kw_gw) <= len(all_gw)
    assert all("NullPointer" in e.message for e in kw_gw)


# ── _format_logs (unit tests) ────────────────────────────────────────────────


def test_format_logs_empty_returns_no_logs_message() -> None:
    result = _format_logs([], "api-gateway", WIDE_START, WIDE_END, 100)
    assert "No log entries found" in result


def test_format_logs_contains_header_with_count(scenario: Scenario) -> None:
    logs = generate_logs(scenario, noise=True)
    gw_logs = [e for e in logs if e.service == "api-gateway"]
    result = _format_logs(gw_logs, "api-gateway", WIDE_START, WIDE_END, len(logs))
    assert f"Found {len(gw_logs)}" in result


def test_format_logs_contains_timestamps(scenario: Scenario) -> None:
    logs = generate_logs(scenario, noise=False)
    gw_logs = [e for e in logs if e.service == "api-gateway"][:3]
    result = _format_logs(gw_logs, "api-gateway", WIDE_START, WIDE_END, 10)
    assert "[2026-05-10" in result


def test_format_logs_contains_levels(scenario: Scenario) -> None:
    logs = generate_logs(scenario, noise=False)
    gw_logs = [e for e in logs if e.service == "api-gateway"]
    result = _format_logs(gw_logs, "api-gateway", WIDE_START, WIDE_END, len(logs))
    assert "ERROR" in result or "WARNING" in result


def test_format_logs_contains_footer_with_total(scenario: Scenario) -> None:
    logs = generate_logs(scenario, noise=True)
    gw_logs = [e for e in logs if e.service == "api-gateway"][:5]
    result = _format_logs(gw_logs, "api-gateway", WIDE_START, WIDE_END, total_raw_count=17)
    assert "17" in result  # total raw count appears in footer
