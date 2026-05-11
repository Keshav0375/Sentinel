"""Tests for sentinel.tools.deploy_checker — list_recent_deploys tool."""

from __future__ import annotations

from datetime import timedelta

import pytest
from agents import FunctionTool

from sentinel.generator.alert_gen import load_scenario
from sentinel.generator.deploy_gen import generate_deploys
from sentinel.generator.scenarios import Scenario
from sentinel.tools.deploy_checker import (
    _filter_deploys,
    _format_deploys,
    _get_reference_time,
    _list_recent_deploys,
    make_deploy_checker_tool,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

# bad_deploy_01 deploy facts (from actual scenario data):
#   deploy-abc123  api-gateway  2026-05-10T02:44:50Z  ci-bot   files:5  (CULPRIT)
#   deploy-user-112 user-service 2026-05-10T01:30:00Z  maya     files:3
#   deploy-xyz789  api-gateway  2026-05-09T14:00:00Z  keshav   files:2  (OLD)
# Reference time (max log ts): 2026-05-10T03:00:01Z
# hours_back=2 window:         2026-05-10T01:00:01Z → 03:00:01Z


@pytest.fixture()
def scenario() -> Scenario:
    return load_scenario("bad_deploy_01")


# ── make_deploy_checker_tool ──────────────────────────────────────────────────


def test_make_returns_function_tool(scenario: Scenario) -> None:
    tool = make_deploy_checker_tool(scenario)
    assert isinstance(tool, FunctionTool)


def test_tool_name_is_list_recent_deploys(scenario: Scenario) -> None:
    tool = make_deploy_checker_tool(scenario)
    assert tool.name == "list_recent_deploys"


def test_tool_has_description(scenario: Scenario) -> None:
    tool = make_deploy_checker_tool(scenario)
    assert tool.description and len(tool.description) > 10


def test_tool_schema_has_service_name(scenario: Scenario) -> None:
    tool = make_deploy_checker_tool(scenario)
    assert "service_name" in str(tool.params_json_schema)


def test_tool_schema_has_hours_back(scenario: Scenario) -> None:
    tool = make_deploy_checker_tool(scenario)
    assert "hours_back" in str(tool.params_json_schema)


def test_tool_scenario_not_in_schema(scenario: Scenario) -> None:
    """Injected scenario dep must NOT appear in the LLM-visible schema."""
    tool = make_deploy_checker_tool(scenario)
    assert "scenario" not in str(tool.params_json_schema)


# ── _list_recent_deploys — error handling ─────────────────────────────────────


@pytest.mark.asyncio
async def test_none_scenario_returns_error() -> None:
    result = await _list_recent_deploys(None, "api-gateway", 2)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_none_scenario_does_not_raise() -> None:
    result = await _list_recent_deploys(None, "api-gateway", 2)
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_empty_service_name_returns_error(scenario: Scenario) -> None:
    result = await _list_recent_deploys(scenario, "", 2)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_zero_hours_back_returns_error(scenario: Scenario) -> None:
    result = await _list_recent_deploys(scenario, "api-gateway", 0)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_negative_hours_back_returns_error(scenario: Scenario) -> None:
    result = await _list_recent_deploys(scenario, "api-gateway", -1)
    assert "ERROR" in result


# ── _list_recent_deploys — successful retrieval ───────────────────────────────


@pytest.mark.asyncio
async def test_returns_string(scenario: Scenario) -> None:
    result = await _list_recent_deploys(scenario, "api-gateway", 2)
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_culprit_deploy_included_in_2h_window(scenario: Scenario) -> None:
    # deploy-abc123 is ~15 min before the incident — should appear with hours_back=2
    result = await _list_recent_deploys(scenario, "api-gateway", 2)
    assert "deploy-abc123" in result


@pytest.mark.asyncio
async def test_old_deploy_excluded_from_2h_window(scenario: Scenario) -> None:
    # deploy-xyz789 is from 2026-05-09 (~13h before) — outside 2h window
    result = await _list_recent_deploys(scenario, "api-gateway", 2)
    assert "deploy-xyz789" not in result


@pytest.mark.asyncio
async def test_old_deploy_included_in_24h_window(scenario: Scenario) -> None:
    # With 24h window, the old api-gateway deploy should appear
    result = await _list_recent_deploys(scenario, "api-gateway", 24)
    assert "deploy-xyz789" in result


@pytest.mark.asyncio
async def test_result_contains_found_count(scenario: Scenario) -> None:
    result = await _list_recent_deploys(scenario, "api-gateway", 2)
    assert "Found 1" in result


@pytest.mark.asyncio
async def test_result_contains_timestamp(scenario: Scenario) -> None:
    result = await _list_recent_deploys(scenario, "api-gateway", 2)
    assert "2026-05-10" in result


@pytest.mark.asyncio
async def test_result_contains_author(scenario: Scenario) -> None:
    result = await _list_recent_deploys(scenario, "api-gateway", 2)
    assert "ci-bot" in result


@pytest.mark.asyncio
async def test_result_contains_commit_sha(scenario: Scenario) -> None:
    result = await _list_recent_deploys(scenario, "api-gateway", 2)
    # commit_sha field should appear in the output
    assert "commit" in result.lower()


@pytest.mark.asyncio
async def test_result_contains_files_changed(scenario: Scenario) -> None:
    result = await _list_recent_deploys(scenario, "api-gateway", 2)
    assert "5" in result  # deploy-abc123 changed 5 files


@pytest.mark.asyncio
async def test_deploys_other_service_excluded(scenario: Scenario) -> None:
    # user-service deploy (deploy-user-112) should not appear for api-gateway
    result = await _list_recent_deploys(scenario, "api-gateway", 2)
    assert "deploy-user-112" not in result


@pytest.mark.asyncio
async def test_user_service_deploy_found_for_user_service(scenario: Scenario) -> None:
    # deploy-user-112 at 01:30:00 — within 2h window for user-service
    result = await _list_recent_deploys(scenario, "user-service", 2)
    assert "deploy-user-112" in result


@pytest.mark.asyncio
async def test_unknown_service_returns_no_deploys_message(scenario: Scenario) -> None:
    result = await _list_recent_deploys(scenario, "nonexistent-svc", 2)
    assert "No deploys found" in result


@pytest.mark.asyncio
async def test_culprit_deploy_id_matches_ground_truth(scenario: Scenario) -> None:
    """The culprit deploy_id in scenario ground truth appears in results."""
    result = await _list_recent_deploys(scenario, "api-gateway", 2)
    assert scenario.ground_truth.deploy_id in result


# ── _get_reference_time ───────────────────────────────────────────────────────


def test_reference_time_is_max_of_logs_and_deploys(scenario: Scenario) -> None:
    ref = _get_reference_time(scenario)
    # Reference should be 2026-05-10T03:00:01Z (latest log ts)
    assert ref.year == 2026
    assert ref.month == 5
    assert ref.day == 10
    assert ref.hour == 3


def test_reference_time_is_timezone_aware(scenario: Scenario) -> None:
    ref = _get_reference_time(scenario)
    assert ref.tzinfo is not None


def test_reference_time_after_culprit_deploy(scenario: Scenario) -> None:
    """Reference time must be after the culprit deploy (it's the incident time)."""
    from sentinel.generator.deploy_gen import generate_deploys

    ref = _get_reference_time(scenario)
    culprit_id = scenario.ground_truth.deploy_id
    deploys = generate_deploys(scenario)
    culprit = next(d for d in deploys if d.id == culprit_id)
    assert ref > culprit.timestamp


# ── _filter_deploys ───────────────────────────────────────────────────────────


def test_filter_by_service_name(scenario: Scenario) -> None:
    all_deploys = generate_deploys(scenario)
    window_start = _get_reference_time(scenario).replace(year=2000)  # very early
    ref = _get_reference_time(scenario)
    result = _filter_deploys(all_deploys, "api-gateway", window_start, ref)
    assert all(d.service == "api-gateway" for d in result)


def test_filter_excludes_deploys_before_window(scenario: Scenario) -> None:
    all_deploys = generate_deploys(scenario)
    ref = _get_reference_time(scenario)
    # Narrow window: only the last 2 hours
    window_start = ref - timedelta(hours=2)
    result = _filter_deploys(all_deploys, "api-gateway", window_start, ref)
    # deploy-xyz789 (2026-05-09T14:00:00) should be excluded
    ids = {d.id for d in result}
    assert "deploy-xyz789" not in ids


def test_filter_keeps_deploys_in_window(scenario: Scenario) -> None:
    all_deploys = generate_deploys(scenario)
    ref = _get_reference_time(scenario)
    window_start = ref - timedelta(hours=2)
    result = _filter_deploys(all_deploys, "api-gateway", window_start, ref)
    # deploy-abc123 should be in window
    ids = {d.id for d in result}
    assert "deploy-abc123" in ids


def test_filter_unknown_service_returns_empty(scenario: Scenario) -> None:
    all_deploys = generate_deploys(scenario)
    ref = _get_reference_time(scenario)
    window_start = ref - timedelta(hours=24)
    result = _filter_deploys(all_deploys, "unknown-svc", window_start, ref)
    assert result == []


# ── _format_deploys ───────────────────────────────────────────────────────────


def test_format_deploys_empty_returns_no_deploys(scenario: Scenario) -> None:
    ref = _get_reference_time(scenario)
    result = _format_deploys([], "api-gateway", 2, ref)
    assert "No deploys found" in result


def test_format_deploys_header_contains_count(scenario: Scenario) -> None:
    all_deploys = generate_deploys(scenario)
    gw_deploys = [d for d in all_deploys if d.service == "api-gateway"][:1]
    ref = _get_reference_time(scenario)
    result = _format_deploys(gw_deploys, "api-gateway", 2, ref)
    assert "Found 1" in result


def test_format_deploys_contains_deploy_id(scenario: Scenario) -> None:
    all_deploys = generate_deploys(scenario)
    gw_deploys = [d for d in all_deploys if d.service == "api-gateway"][:1]
    ref = _get_reference_time(scenario)
    result = _format_deploys(gw_deploys, "api-gateway", 2, ref)
    assert gw_deploys[0].id in result


def test_format_deploys_contains_all_fields(scenario: Scenario) -> None:
    all_deploys = generate_deploys(scenario)
    gw_deploys = [d for d in all_deploys if d.service == "api-gateway"][:1]
    ref = _get_reference_time(scenario)
    result = _format_deploys(gw_deploys, "api-gateway", 2, ref)
    assert "timestamp" in result
    assert "author" in result
    assert "commit" in result
    assert "files_changed" in result


def test_format_deploys_multiline_for_multiple_deploys(scenario: Scenario) -> None:
    all_deploys = generate_deploys(scenario)
    # Use all api-gateway deploys (culprit + old)
    gw_deploys = [d for d in all_deploys if d.service == "api-gateway"]
    ref = _get_reference_time(scenario)
    result = _format_deploys(gw_deploys, "api-gateway", 24, ref)
    assert "deploy-abc123" in result
    assert "deploy-xyz789" in result
