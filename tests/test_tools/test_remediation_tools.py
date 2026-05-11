"""Tests for sentinel.tools.remediation_tools — draft_rollback_pr and draft_hotfix."""

from __future__ import annotations

import pytest
from agents import FunctionTool

from sentinel.tools.remediation_tools import (
    _draft_hotfix,
    _draft_rollback_pr,
    make_remediation_tools,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def tools() -> list[FunctionTool]:
    return make_remediation_tools()


@pytest.fixture()
def rollback_tool(tools: list[FunctionTool]) -> FunctionTool:
    return next(t for t in tools if t.name == "draft_rollback_pr")


@pytest.fixture()
def hotfix_tool(tools: list[FunctionTool]) -> FunctionTool:
    return next(t for t in tools if t.name == "draft_hotfix")


# ── make_remediation_tools — list structure ───────────────────────────────────


def test_make_returns_list(tools: list[FunctionTool]) -> None:
    assert isinstance(tools, list)


def test_make_returns_two_tools(tools: list[FunctionTool]) -> None:
    assert len(tools) == 2


def test_all_items_are_function_tools(tools: list[FunctionTool]) -> None:
    assert all(isinstance(t, FunctionTool) for t in tools)


def test_rollback_tool_present(tools: list[FunctionTool]) -> None:
    names = {t.name for t in tools}
    assert "draft_rollback_pr" in names


def test_hotfix_tool_present(tools: list[FunctionTool]) -> None:
    names = {t.name for t in tools}
    assert "draft_hotfix" in names


# ── draft_rollback_pr — tool wrapping ────────────────────────────────────────


def test_rollback_name(rollback_tool: FunctionTool) -> None:
    assert rollback_tool.name == "draft_rollback_pr"


def test_rollback_has_description(rollback_tool: FunctionTool) -> None:
    assert rollback_tool.description and len(rollback_tool.description) > 10


def test_rollback_schema_has_deploy_id(rollback_tool: FunctionTool) -> None:
    assert "deploy_id" in str(rollback_tool.params_json_schema)


def test_rollback_schema_has_justification(rollback_tool: FunctionTool) -> None:
    assert "justification" in str(rollback_tool.params_json_schema)


# ── draft_hotfix — tool wrapping ─────────────────────────────────────────────


def test_hotfix_name(hotfix_tool: FunctionTool) -> None:
    assert hotfix_tool.name == "draft_hotfix"


def test_hotfix_has_description(hotfix_tool: FunctionTool) -> None:
    assert hotfix_tool.description and len(hotfix_tool.description) > 10


def test_hotfix_schema_has_file_path(hotfix_tool: FunctionTool) -> None:
    assert "file_path" in str(hotfix_tool.params_json_schema)


def test_hotfix_schema_has_fix_description(hotfix_tool: FunctionTool) -> None:
    assert "fix_description" in str(hotfix_tool.params_json_schema)


# ── _draft_rollback_pr — error handling ──────────────────────────────────────


def test_rollback_empty_deploy_id_returns_error() -> None:
    result = _draft_rollback_pr("", "bad jwt auth change")
    assert "ERROR" in result


def test_rollback_whitespace_deploy_id_returns_error() -> None:
    result = _draft_rollback_pr("   ", "bad jwt auth change")
    assert "ERROR" in result


def test_rollback_empty_justification_returns_error() -> None:
    result = _draft_rollback_pr("deploy-abc123", "")
    assert "ERROR" in result


def test_rollback_whitespace_justification_returns_error() -> None:
    result = _draft_rollback_pr("deploy-abc123", "   ")
    assert "ERROR" in result


# ── _draft_rollback_pr — output content ──────────────────────────────────────


def test_rollback_returns_string() -> None:
    result = _draft_rollback_pr("deploy-abc123", "NullPointerException in AuthMiddleware")
    assert isinstance(result, str)


def test_rollback_contains_deploy_id() -> None:
    result = _draft_rollback_pr("deploy-abc123", "NullPointerException in AuthMiddleware")
    assert "deploy-abc123" in result


def test_rollback_contains_justification() -> None:
    justification = "NullPointerException in AuthMiddleware after this deploy"
    result = _draft_rollback_pr("deploy-abc123", justification)
    assert justification in result


def test_rollback_contains_title() -> None:
    result = _draft_rollback_pr("deploy-abc123", "bad deploy")
    assert "title" in result.lower() or "revert" in result.lower()


def test_rollback_contains_branch() -> None:
    result = _draft_rollback_pr("deploy-abc123", "bad deploy")
    assert "revert/deploy-abc123" in result


def test_rollback_contains_target_main() -> None:
    result = _draft_rollback_pr("deploy-abc123", "bad deploy")
    assert "main" in result


def test_rollback_contains_checklist() -> None:
    result = _draft_rollback_pr("deploy-abc123", "bad deploy")
    assert "checklist" in result.lower() or "[ ]" in result


def test_rollback_contains_approval_warning() -> None:
    result = _draft_rollback_pr("deploy-abc123", "bad deploy")
    assert "approval" in result.lower() or "⚠" in result


def test_rollback_does_not_raise() -> None:
    result = _draft_rollback_pr("deploy-abc123", "bad deploy")
    assert result  # non-empty


def test_rollback_different_deploy_ids_differ() -> None:
    r1 = _draft_rollback_pr("deploy-aaa", "bad deploy")
    r2 = _draft_rollback_pr("deploy-bbb", "bad deploy")
    assert "deploy-aaa" in r1
    assert "deploy-bbb" in r2
    assert "deploy-aaa" not in r2


# ── _draft_hotfix — error handling ───────────────────────────────────────────


def test_hotfix_empty_file_path_returns_error() -> None:
    result = _draft_hotfix("", "add null check for config key")
    assert "ERROR" in result


def test_hotfix_whitespace_file_path_returns_error() -> None:
    result = _draft_hotfix("   ", "add null check for config key")
    assert "ERROR" in result


def test_hotfix_empty_fix_description_returns_error() -> None:
    result = _draft_hotfix("src/auth/middleware.py", "")
    assert "ERROR" in result


def test_hotfix_whitespace_fix_description_returns_error() -> None:
    result = _draft_hotfix("src/auth/middleware.py", "   ")
    assert "ERROR" in result


# ── _draft_hotfix — output content ───────────────────────────────────────────


def test_hotfix_returns_string() -> None:
    result = _draft_hotfix("src/auth/middleware.py", "add null check for config key")
    assert isinstance(result, str)


def test_hotfix_contains_file_path() -> None:
    result = _draft_hotfix("src/auth/middleware.py", "add null check for config key")
    assert "src/auth/middleware.py" in result


def test_hotfix_contains_fix_description() -> None:
    fix = "add null check for config key before JWT validation"
    result = _draft_hotfix("src/auth/middleware.py", fix)
    assert fix in result


def test_hotfix_contains_diff_markers() -> None:
    result = _draft_hotfix("src/auth/middleware.py", "add null check")
    assert "---" in result and "+++" in result


def test_hotfix_contains_test_suggestions() -> None:
    result = _draft_hotfix("src/auth/middleware.py", "add null check")
    assert "test" in result.lower()


def test_hotfix_contains_approval_warning() -> None:
    result = _draft_hotfix("src/auth/middleware.py", "add null check")
    assert "approval" in result.lower() or "⚠" in result


def test_hotfix_does_not_raise() -> None:
    result = _draft_hotfix("src/auth/middleware.py", "add null check")
    assert result  # non-empty


def test_hotfix_different_files_differ() -> None:
    r1 = _draft_hotfix("src/auth/middleware.py", "add null check")
    r2 = _draft_hotfix("src/payment/processor.py", "add null check")
    assert "src/auth/middleware.py" in r1
    assert "src/payment/processor.py" in r2
    assert "src/payment/processor.py" not in r1
