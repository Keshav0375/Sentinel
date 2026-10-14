"""Tests for sentinel.tools.remediation_tools — prepare_rollback_spec and format_escalation.

Both tools are output-only (backend.md §3.3: the backend drafts, GitHub Actions executes,
a human merges the revert PR). The tests check the drafted shapes — the rollback spec is
the §3.4 ``/generate/pr-content`` body field for field — the bad_input failures, and
that no call opens a socket, an HTTP client or a database connection.
"""

from __future__ import annotations

import ast
import json
import socket
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from agents import FunctionTool
from agents.tool_context import ToolContext

from sentinel.models.tools import (
    EscalationContext,
    EscalationInput,
    RollbackSpec,
    RollbackSpecInput,
    TargetDeploy,
    ToolFailure,
)
from sentinel.tools import remediation_tools
from sentinel.tools.remediation_tools import (
    _format_escalation,
    _prepare_rollback_spec,
    make_remediation_tools,
)

# The §3.4 POST /generate/pr-content example body (backend.md), with the commit SHA
# lengthened to 7 characters — the shortest abbreviation git prints and the tool accepts.
PR_CONTENT_BODY: dict[str, Any] = {
    "incident_id": "inc-uuid",
    "root_cause": "Deploy pr-5-a3f9c2 changed /health to return 503 when any dependency "
    "is degraded",
    "evidence_summary": "Verify stage failed 3/3 health checks post-deploy; Datadog logs "
    "show 503s starting at deploy time; no other deploys in the window",
    "confidence": 0.85,
    "target_deploy": {
        "pr_number": 5,
        "commit_sha": "a3f9c2e",
        "deployed_at": "2026-07-01T12:00:00Z",
    },
    "resolution_type": "rollback",
}

JUSTIFICATION = "The only deploy in the window; 503s begin at its deploy time."


def _rollback_args(**overrides: Any) -> dict[str, Any]:
    """The tool arguments that draft PR_CONTENT_BODY (plus the justification)."""
    args = {k: v for k, v in PR_CONTENT_BODY.items() if k != "resolution_type"}
    args["target_deploy"] = dict(PR_CONTENT_BODY["target_deploy"])
    args["justification"] = JUSTIFICATION
    target = overrides.pop("target_deploy", {})
    args["target_deploy"].update(target)
    args.update(overrides)
    return args


def _spec(result: RollbackSpec | ToolFailure) -> RollbackSpec:
    assert isinstance(result, RollbackSpec), result
    return result


def _escalation(result: EscalationContext | ToolFailure) -> EscalationContext:
    assert isinstance(result, EscalationContext), result
    return result


def _bad_input(result: object, tool: str) -> ToolFailure:
    assert isinstance(result, ToolFailure), result
    assert (result.tool, result.kind) == (tool, "bad_input")
    return result


async def _invoke(
    tool: FunctionTool, arguments: dict[str, Any]
) -> RollbackSpec | EscalationContext | ToolFailure:
    """Call the tool the way the Runner does: JSON arguments through on_invoke_tool.

    The model reads compact JSON back; it is decoded here into the typed result.
    """
    raw = json.dumps(arguments)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="call-1", tool_arguments=raw)
    text = await tool.on_invoke_tool(ctx, raw)
    assert isinstance(text, str), text
    data = json.loads(text)
    if data.get("error") is True:
        return ToolFailure.model_validate(data)
    if tool.name == "prepare_rollback_spec":
        return RollbackSpec.model_validate(data)
    return EscalationContext.model_validate(data)


@pytest.fixture()
def tools() -> dict[str, FunctionTool]:
    return {tool.name: tool for tool in make_remediation_tools()}


# ── The tools ─────────────────────────────────────────────────────────────────


def test_factory_returns_exactly_the_two_output_only_tools(
    tools: dict[str, FunctionTool],
) -> None:
    assert list(tools) == ["prepare_rollback_spec", "format_escalation"]
    for tool in tools.values():
        assert isinstance(tool, FunctionTool)
        assert list(tool.params_json_schema["properties"]) == ["request"]


def test_rollback_schema_exposes_the_spec_fields(tools: dict[str, FunctionTool]) -> None:
    schema = json.dumps(tools["prepare_rollback_spec"].params_json_schema)
    for field in (
        "incident_id",
        "root_cause",
        "evidence_summary",
        "confidence",
        "justification",
        "target_deploy",
        "pr_number",
        "commit_sha",
        "deployed_at",
    ):
        assert field in schema
    # The model never chooses the resolution type: the spec is always a rollback.
    assert "resolution_type" not in schema


def test_descriptions_say_output_only_and_human_merge(tools: dict[str, FunctionTool]) -> None:
    rollback = tools["prepare_rollback_spec"].description.lower()
    for phrase in ("output only", "opens no pr", "human merges", "bad_input"):
        assert phrase in rollback
    escalation = tools["format_escalation"].description.lower()
    for phrase in ("output only", "notifies no one", "missing", "bad_input"):
        assert phrase in escalation


# ── prepare_rollback_spec ─────────────────────────────────────────────────────


def test_spec_from_root_cause_and_target_deploy() -> None:
    spec = _spec(_prepare_rollback_spec(RollbackSpecInput.model_validate(_rollback_args())))
    assert spec.incident_id == "inc-uuid"
    assert spec.root_cause.startswith("Deploy pr-5-a3f9c2")
    assert spec.confidence == 0.85
    assert spec.justification == JUSTIFICATION
    assert spec.resolution_type == "rollback"
    assert spec.target_deploy == TargetDeploy(
        pr_number=5, commit_sha="a3f9c2e", deployed_at=datetime(2026, 7, 1, 12, tzinfo=UTC)
    )


def test_spec_json_is_the_pr_content_body_field_for_field() -> None:
    """The spec serialises to the §3.4 body plus the justification — nothing else."""
    spec = _spec(_prepare_rollback_spec(RollbackSpecInput.model_validate(_rollback_args())))
    body = json.loads(spec.model_dump_json())
    assert body == {**PR_CONTENT_BODY, "justification": JUSTIFICATION}
    assert body["target_deploy"]["deployed_at"] == "2026-07-01T12:00:00Z"


def test_target_sha_is_the_commit_the_workflow_reverts() -> None:
    """The workflow's TARGET_SHA reads target_deploy.commit_sha, normalised to lower case."""
    full = "A3F9C2E1B4D5F6A7B8C9D0E1F2A3B4C5D6E7F8A9"
    spec = _spec(
        _prepare_rollback_spec(
            RollbackSpecInput.model_validate(
                _rollback_args(target_deploy={"commit_sha": f"  {full} "})
            )
        )
    )
    assert spec.target_deploy.commit_sha == full.lower()


def test_spec_strips_text_and_normalises_deploy_time_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    request = RollbackSpecInput.model_validate(
        _rollback_args(
            incident_id="  inc-7  ",
            root_cause=" cause ",
            target_deploy={"deployed_at": datetime(2026, 7, 1, 14, tzinfo=plus_two).isoformat()},
        )
    )
    spec = _spec(_prepare_rollback_spec(request))
    assert (spec.incident_id, spec.root_cause) == ("inc-7", "cause")
    assert spec.target_deploy.deployed_at == datetime(2026, 7, 1, 12, tzinfo=UTC)
    assert spec.target_deploy.deployed_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("confidence", [0.0, 0.3, 1.0])
def test_spec_drafts_at_any_confidence(confidence: float) -> None:
    """No threshold in the tool: whether a spec is acted on is the orchestrator's rule."""
    spec = _spec(
        _prepare_rollback_spec(
            RollbackSpecInput.model_validate(_rollback_args(confidence=confidence))
        )
    )
    assert spec.confidence == confidence


@pytest.mark.parametrize(
    ("overrides", "names"),
    [
        ({"target_deploy": {"commit_sha": "a3f9c2"}}, "commit_sha"),  # 6: too short
        ({"target_deploy": {"commit_sha": "a" * 41}}, "commit_sha"),  # 41: too long
        ({"target_deploy": {"commit_sha": "a3f9c2g"}}, "commit_sha"),  # not hex
        ({"target_deploy": {"commit_sha": "HEAD~1"}}, "commit_sha"),
        ({"target_deploy": {"commit_sha": "a3f9c2e; rm -rf /"}}, "commit_sha"),
        ({"target_deploy": {"commit_sha": ""}}, "commit_sha"),
        ({"target_deploy": {"pr_number": 0}}, "pr_number"),
        ({"target_deploy": {"deployed_at": "2026-07-01T12:00:00"}}, "timezone"),
        ({"confidence": -0.01}, "confidence"),
        ({"confidence": 1.01}, "confidence"),
        ({"confidence": float("nan")}, "confidence"),
        ({"confidence": float("inf")}, "confidence"),
        ({"incident_id": "  "}, "incident_id"),
        ({"root_cause": ""}, "root_cause"),
        ({"evidence_summary": "\n"}, "evidence_summary"),
        ({"justification": ""}, "justification"),
    ],
)
def test_bad_rollback_input_is_refused(overrides: dict[str, Any], names: str) -> None:
    request = RollbackSpecInput.model_validate(_rollback_args(**overrides))
    failure = _bad_input(_prepare_rollback_spec(request), "prepare_rollback_spec")
    assert names in failure.message


def test_spec_model_rejects_confidence_out_of_range() -> None:
    """The output model holds the range too, for any other code that builds one."""
    with pytest.raises(ValueError):
        RollbackSpec.model_validate({**PR_CONTENT_BODY, "justification": "j", "confidence": 2})


# ── format_escalation ─────────────────────────────────────────────────────────


def test_escalation_formats_full_context() -> None:
    context = _escalation(
        _format_escalation(
            EscalationInput(
                hypothesis="  Upstream dependency degraded; no deploy in the window.  ",
                evidence=[
                    "3/3 synthetic checks failed from 12:02 UTC",
                    "  ",
                    "no deploy in the last 24 h",
                    "3/3 synthetic checks failed from 12:02 UTC",
                ],
                confidence=0.4,
                missing=[" CI logs unavailable (Datadog key not set) ", ""],
            )
        )
    )
    assert context == EscalationContext(
        hypothesis="Upstream dependency degraded; no deploy in the window.",
        evidence=["3/3 synthetic checks failed from 12:02 UTC", "no deploy in the last 24 h"],
        confidence=0.4,
        missing=["CI logs unavailable (Datadog key not set)"],
    )
    assert set(json.loads(context.model_dump_json())) == {
        "hypothesis",
        "evidence",
        "confidence",
        "missing",
    }


def test_escalation_with_nothing_found_keeps_empty_lists() -> None:
    context = _escalation(
        _format_escalation(EscalationInput(hypothesis="Unknown cause.", confidence=0.0))
    )
    assert (context.evidence, context.missing) == ([], [])


@pytest.mark.parametrize(
    ("overrides", "names"),
    [
        ({"hypothesis": "   "}, "hypothesis"),
        ({"confidence": -1.0}, "confidence"),
        ({"confidence": 1.5}, "confidence"),
        ({"confidence": float("nan")}, "confidence"),
    ],
)
def test_bad_escalation_input_is_refused(overrides: dict[str, Any], names: str) -> None:
    fields: dict[str, Any] = {"hypothesis": "h", "confidence": 0.5, **overrides}
    failure = _bad_input(_format_escalation(EscalationInput(**fields)), "format_escalation")
    assert names in failure.message


# ── on_invoke_tool (the Runner's path) ────────────────────────────────────────


async def test_on_invoke_rollback_with_json_args(tools: dict[str, FunctionTool]) -> None:
    result = await _invoke(tools["prepare_rollback_spec"], {"request": _rollback_args()})
    assert json.loads(_spec(result).model_dump_json())["target_deploy"]["commit_sha"] == "a3f9c2e"


async def test_on_invoke_rollback_bad_sha_returns_failure(
    tools: dict[str, FunctionTool],
) -> None:
    args = _rollback_args(target_deploy={"commit_sha": "not-a-sha"})
    result = await _invoke(tools["prepare_rollback_spec"], {"request": args})
    _bad_input(result, "prepare_rollback_spec")


async def test_on_invoke_escalation_with_json_args(tools: dict[str, FunctionTool]) -> None:
    result = await _invoke(
        tools["format_escalation"],
        {"request": {"hypothesis": "h", "evidence": ["e"], "confidence": 0.2}},
    )
    assert _escalation(result).missing == []


# ── No I/O ────────────────────────────────────────────────────────────────────


class _IOAttempted(AssertionError):
    pass


def _refuse(*_args: Any, **_kwargs: Any) -> Any:
    raise _IOAttempted("a pure tool attempted I/O")


async def test_tools_touch_no_network_socket_or_database(
    tools: dict[str, FunctionTool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With sockets, HTTP clients and DB connections all raising, both tools still work."""
    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", _refuse)
    monkeypatch.setattr(httpx.Client, "send", _refuse)
    monkeypatch.setattr(asyncpg, "connect", _refuse)
    monkeypatch.setattr(asyncpg, "create_pool", _refuse)

    first = await _invoke(tools["prepare_rollback_spec"], {"request": _rollback_args()})
    again = await _invoke(tools["prepare_rollback_spec"], {"request": _rollback_args()})
    assert _spec(first) == _spec(again)  # same input, same output: no clock, no state
    failure = await _invoke(
        tools["prepare_rollback_spec"], {"request": _rollback_args(confidence=7)}
    )
    _bad_input(failure, "prepare_rollback_spec")
    escalation = await _invoke(
        tools["format_escalation"], {"request": {"hypothesis": "h", "confidence": 0.1}}
    )
    _escalation(escalation)

    with pytest.raises(_IOAttempted):  # the patches are live: I/O would have raised
        socket.socket()


def test_module_imports_no_io_library() -> None:
    """The module can reach no network, database, clock or logger: it imports none."""
    tree = ast.parse(Path(remediation_tools.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported == {
        "__future__",
        "math",
        "re",
        "datetime",
        "agents",
        "sentinel.models.tools",
        "sentinel.tools.output",
    }
