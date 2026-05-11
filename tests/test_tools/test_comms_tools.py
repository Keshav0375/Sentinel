"""Tests for sentinel.tools.comms_tools — draft_slack_summary tool."""

from __future__ import annotations

import json

import pytest
from agents import FunctionTool

from sentinel.tools.comms_tools import (
    _draft_slack_summary,
    _format_slack_message,
    _parse_incident_data,
    make_comms_tools,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────

SAMPLE_JSON = json.dumps(
    {
        "service": "api-gateway",
        "severity": "P1",
        "impact": "500 errors on /api/v1/users, 40% of requests affected",
        "root_cause": "NullPointerException in AuthMiddleware from deploy-abc123",
        "timeline": "02:44 deploy | 02:45 errors | 02:50 alert | 03:00 investigating",
        "status": "Rollback PR drafted, awaiting approval",
        "action_items": "Review rollback PR, monitor error rate post-merge",
        "eta": "15 minutes",
    }
)


@pytest.fixture()
def tools() -> list[FunctionTool]:
    return make_comms_tools()


@pytest.fixture()
def summary_tool(tools: list[FunctionTool]) -> FunctionTool:
    return tools[0]


# ── make_comms_tools ─────────────────────────────────────────────────────────


def test_make_returns_list(tools: list[FunctionTool]) -> None:
    assert isinstance(tools, list)


def test_make_returns_one_tool(tools: list[FunctionTool]) -> None:
    assert len(tools) == 1


def test_tool_is_function_tool(summary_tool: FunctionTool) -> None:
    assert isinstance(summary_tool, FunctionTool)


def test_tool_name(summary_tool: FunctionTool) -> None:
    assert summary_tool.name == "draft_slack_summary"


def test_tool_has_description(summary_tool: FunctionTool) -> None:
    assert summary_tool.description and len(summary_tool.description) > 10


def test_tool_schema_has_incident_summary(summary_tool: FunctionTool) -> None:
    assert "incident_summary" in str(summary_tool.params_json_schema)


# ── _draft_slack_summary — error handling ────────────────────────────────────


def test_empty_summary_returns_error() -> None:
    result = _draft_slack_summary("")
    assert "ERROR" in result


def test_whitespace_summary_returns_error() -> None:
    result = _draft_slack_summary("   ")
    assert "ERROR" in result


# ── _draft_slack_summary — JSON input ────────────────────────────────────────


def test_json_returns_string() -> None:
    result = _draft_slack_summary(SAMPLE_JSON)
    assert isinstance(result, str)


def test_json_contains_service_name() -> None:
    result = _draft_slack_summary(SAMPLE_JSON)
    assert "api-gateway" in result


def test_json_contains_severity() -> None:
    result = _draft_slack_summary(SAMPLE_JSON)
    assert "P1" in result


def test_json_contains_impact() -> None:
    result = _draft_slack_summary(SAMPLE_JSON)
    assert "500 errors" in result


def test_json_contains_root_cause() -> None:
    result = _draft_slack_summary(SAMPLE_JSON)
    assert "NullPointerException" in result


def test_json_contains_timeline() -> None:
    result = _draft_slack_summary(SAMPLE_JSON)
    assert "02:44" in result


def test_json_contains_status() -> None:
    result = _draft_slack_summary(SAMPLE_JSON)
    assert "Rollback PR drafted" in result


def test_json_contains_action_items() -> None:
    result = _draft_slack_summary(SAMPLE_JSON)
    assert "monitor error rate" in result


def test_json_contains_eta() -> None:
    result = _draft_slack_summary(SAMPLE_JSON)
    assert "15 minutes" in result


def test_json_contains_all_section_headers() -> None:
    result = _draft_slack_summary(SAMPLE_JSON)
    assert "*Impact*" in result
    assert "*Root Cause*" in result
    assert "*Timeline*" in result
    assert "*Current Status*" in result
    assert "*Action Items*" in result
    assert "*ETA to Resolution*" in result


# ── _draft_slack_summary — raw text fallback ─────────────────────────────────


def test_raw_text_returns_string() -> None:
    result = _draft_slack_summary("api-gateway is returning 500s")
    assert isinstance(result, str)


def test_raw_text_contains_input() -> None:
    text = "api-gateway is returning 500s after deploy"
    result = _draft_slack_summary(text)
    assert text in result


def test_raw_text_has_section_headers() -> None:
    result = _draft_slack_summary("some incident happened")
    assert "*Impact*" in result
    assert "*Root Cause*" in result
    assert "*Timeline*" in result


def test_raw_text_uses_na_for_missing_fields() -> None:
    result = _draft_slack_summary("error spike on payment-service")
    assert "N/A" in result


# ── _draft_slack_summary — partial JSON ──────────────────────────────────────


def test_partial_json_fills_missing_with_na() -> None:
    partial = json.dumps({"service": "payment-service", "severity": "P2"})
    result = _draft_slack_summary(partial)
    assert "payment-service" in result
    assert "P2" in result
    assert "N/A" in result


def test_json_array_falls_back_to_raw_text() -> None:
    result = _draft_slack_summary('["not", "a", "dict"]')
    assert "*Impact*" in result


# ── _parse_incident_data ────────────────────────────────────────────────────


def test_parse_json_extracts_service() -> None:
    data = _parse_incident_data(SAMPLE_JSON)
    assert data["service"] == "api-gateway"


def test_parse_json_extracts_all_keys() -> None:
    data = _parse_incident_data(SAMPLE_JSON)
    expected_keys = {
        "service", "severity", "impact", "root_cause",
        "timeline", "status", "action_items", "eta",
    }
    assert set(data.keys()) == expected_keys


def test_parse_raw_text_puts_text_in_timeline() -> None:
    data = _parse_incident_data("error spike on api-gateway")
    assert "error spike on api-gateway" in data["timeline"]


def test_parse_raw_text_service_is_na() -> None:
    data = _parse_incident_data("some errors happened")
    assert data["service"] == "N/A"


def test_parse_missing_key_returns_na() -> None:
    partial = json.dumps({"service": "api-gateway"})
    data = _parse_incident_data(partial)
    assert data["impact"] == "N/A"


# ── _format_slack_message ────────────────────────────────────────────────────


def test_format_contains_emoji() -> None:
    data = {
        "service": "api-gateway", "severity": "P1",
        "impact": "errors", "root_cause": "bad deploy",
        "timeline": "events", "status": "investigating",
        "action_items": "rollback", "eta": "10 min",
    }
    result = _format_slack_message(data)
    assert ":rotating_light:" in result


def test_format_header_has_service_and_severity() -> None:
    data = {
        "service": "payment-service", "severity": "P2",
        "impact": "x", "root_cause": "x",
        "timeline": "x", "status": "x",
        "action_items": "x", "eta": "x",
    }
    result = _format_slack_message(data)
    assert "payment-service" in result
    assert "P2" in result


def test_format_all_sections_present() -> None:
    data = {
        "service": "s", "severity": "P3",
        "impact": "some impact", "root_cause": "some cause",
        "timeline": "some timeline", "status": "some status",
        "action_items": "some items", "eta": "some eta",
    }
    result = _format_slack_message(data)
    assert "some impact" in result
    assert "some cause" in result
    assert "some timeline" in result
    assert "some status" in result
    assert "some items" in result
    assert "some eta" in result
