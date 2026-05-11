"""Tests for sentinel.tools.hitl — request_human_approval HITL gate."""

from __future__ import annotations

import pytest
from agents import FunctionTool

from sentinel.tools.hitl import (
    _format_request_display,
    _format_result,
    _request_human_approval,
    make_hitl_tool,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

ACTION = "Roll back deploy-abc123 on api-gateway"
RISK = "high"
EVIDENCE = "NullPointerException appeared 5s after deploy-abc123"
PROPOSER = "remediation_agent"


async def _auto_approve(display: str) -> tuple[str, str | None]:
    return "approve", None


async def _auto_reject(display: str) -> tuple[str, str | None]:
    return "reject", "Need more evidence"


async def _auto_reject_no_comment(display: str) -> tuple[str, str | None]:
    return "reject", None


# ── make_hitl_tool — tool wrapping ───────────────────────────────────────────


def test_make_returns_function_tool() -> None:
    tool = make_hitl_tool(approval_fn=_auto_approve)
    assert isinstance(tool, FunctionTool)


def test_tool_name() -> None:
    tool = make_hitl_tool(approval_fn=_auto_approve)
    assert tool.name == "request_human_approval"


def test_tool_has_description() -> None:
    tool = make_hitl_tool(approval_fn=_auto_approve)
    assert tool.description and len(tool.description) > 10


def test_tool_schema_has_action() -> None:
    tool = make_hitl_tool(approval_fn=_auto_approve)
    assert "action" in str(tool.params_json_schema)


def test_tool_schema_has_risk_level() -> None:
    tool = make_hitl_tool(approval_fn=_auto_approve)
    assert "risk_level" in str(tool.params_json_schema)


def test_tool_schema_has_evidence_summary() -> None:
    tool = make_hitl_tool(approval_fn=_auto_approve)
    assert "evidence_summary" in str(tool.params_json_schema)


def test_tool_schema_has_proposed_by() -> None:
    tool = make_hitl_tool(approval_fn=_auto_approve)
    assert "proposed_by" in str(tool.params_json_schema)


# ── _request_human_approval — error handling ─────────────────────────────────


@pytest.mark.asyncio
async def test_empty_action_returns_error() -> None:
    result = await _request_human_approval("", RISK, EVIDENCE, PROPOSER, _auto_approve)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_whitespace_action_returns_error() -> None:
    result = await _request_human_approval("   ", RISK, EVIDENCE, PROPOSER, _auto_approve)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_empty_risk_level_returns_error() -> None:
    result = await _request_human_approval(ACTION, "", EVIDENCE, PROPOSER, _auto_approve)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_empty_evidence_returns_error() -> None:
    result = await _request_human_approval(ACTION, RISK, "", PROPOSER, _auto_approve)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_empty_proposed_by_returns_error() -> None:
    result = await _request_human_approval(ACTION, RISK, EVIDENCE, "", _auto_approve)
    assert "ERROR" in result


# ── _request_human_approval — approved ───────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_returns_string() -> None:
    result = await _request_human_approval(
        ACTION, RISK, EVIDENCE, PROPOSER, _auto_approve
    )
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_approve_contains_approved_status() -> None:
    result = await _request_human_approval(
        ACTION, RISK, EVIDENCE, PROPOSER, _auto_approve
    )
    assert "APPROVED" in result


@pytest.mark.asyncio
async def test_approve_contains_action() -> None:
    result = await _request_human_approval(
        ACTION, RISK, EVIDENCE, PROPOSER, _auto_approve
    )
    assert ACTION in result


@pytest.mark.asyncio
async def test_approve_contains_risk_level() -> None:
    result = await _request_human_approval(
        ACTION, RISK, EVIDENCE, PROPOSER, _auto_approve
    )
    assert RISK in result


@pytest.mark.asyncio
async def test_approve_contains_proposed_by() -> None:
    result = await _request_human_approval(
        ACTION, RISK, EVIDENCE, PROPOSER, _auto_approve
    )
    assert PROPOSER in result


# ── _request_human_approval — rejected ───────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_contains_rejected_status() -> None:
    result = await _request_human_approval(
        ACTION, RISK, EVIDENCE, PROPOSER, _auto_reject
    )
    assert "REJECTED" in result


@pytest.mark.asyncio
async def test_reject_contains_comment() -> None:
    result = await _request_human_approval(
        ACTION, RISK, EVIDENCE, PROPOSER, _auto_reject
    )
    assert "Need more evidence" in result


@pytest.mark.asyncio
async def test_reject_no_comment_shows_none() -> None:
    result = await _request_human_approval(
        ACTION, RISK, EVIDENCE, PROPOSER, _auto_reject_no_comment
    )
    assert "none" in result.lower()


@pytest.mark.asyncio
async def test_unknown_decision_treated_as_reject() -> None:
    async def _unknown(display: str) -> tuple[str, str | None]:
        return "maybe", None

    result = await _request_human_approval(
        ACTION, RISK, EVIDENCE, PROPOSER, _unknown
    )
    assert "REJECTED" in result


# ── _request_human_approval — callback receives display ──────────────────────


@pytest.mark.asyncio
async def test_callback_receives_display_string() -> None:
    captured: list[str] = []

    async def _capture(display: str) -> tuple[str, str | None]:
        captured.append(display)
        return "approve", None

    await _request_human_approval(ACTION, RISK, EVIDENCE, PROPOSER, _capture)
    assert len(captured) == 1
    assert ACTION in captured[0]
    assert EVIDENCE in captured[0]


# ── _format_request_display ──────────────────────────────────────────────────


def test_display_contains_action() -> None:
    result = _format_request_display(ACTION, RISK, EVIDENCE, PROPOSER)
    assert ACTION in result


def test_display_contains_evidence() -> None:
    result = _format_request_display(ACTION, RISK, EVIDENCE, PROPOSER)
    assert EVIDENCE in result


def test_display_contains_risk_level() -> None:
    result = _format_request_display(ACTION, RISK, EVIDENCE, PROPOSER)
    assert RISK in result


def test_display_contains_proposed_by() -> None:
    result = _format_request_display(ACTION, RISK, EVIDENCE, PROPOSER)
    assert PROPOSER in result


def test_display_high_risk_has_triple_bang() -> None:
    result = _format_request_display(ACTION, "high", EVIDENCE, PROPOSER)
    assert "!!!" in result


def test_display_low_risk_has_single_bang() -> None:
    result = _format_request_display(ACTION, "low", EVIDENCE, PROPOSER)
    assert "!" in result
    assert "!!!" not in result


def test_display_has_separator_lines() -> None:
    result = _format_request_display(ACTION, RISK, EVIDENCE, PROPOSER)
    assert "=" * 60 in result


# ── _format_result ───────────────────────────────────────────────────────────


def test_result_approved_format() -> None:
    result = _format_result(ACTION, RISK, PROPOSER, "APPROVED", None)
    assert "APPROVED" in result
    assert ACTION in result
    assert "none" in result.lower()


def test_result_rejected_with_comment() -> None:
    result = _format_result(ACTION, RISK, PROPOSER, "REJECTED", "bad idea")
    assert "REJECTED" in result
    assert "bad idea" in result


def test_result_contains_all_fields() -> None:
    result = _format_result(ACTION, RISK, PROPOSER, "APPROVED", None)
    assert "action:" in result
    assert "risk_level:" in result
    assert "proposed_by:" in result
    assert "status:" in result
    assert "comment:" in result
