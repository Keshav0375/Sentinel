"""Tests for sentinel.tools.service_lookup — get_service_metadata tool.

Unit tests: no database. The tool is exercised against an in-memory stand-in for
SemanticMemory, and against a real SemanticMemory with no pool for the "memory
unavailable" path. The tool over the seeded ``services`` table (sentinel-watchtower, a
NULL tier) is covered in tests/test_memory/test_semantic.py.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from agents import FunctionTool
from agents.tool_context import ToolContext
from pydantic import ValidationError

from sentinel.exceptions import SentinelMemoryError
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.memory import Runbook, SemanticRecord
from sentinel.models.service import ServiceMetadata, ServiceTier
from sentinel.models.tools import ServiceLookupInput, ToolFailure
from sentinel.tools.service_lookup import _lookup_service, make_service_lookup_tool


class _InMemorySemantic(SemanticMemory):
    """SemanticMemory stand-in: records in a dict. Only query (the tool's call) is overridden."""

    def __init__(self, records: dict[str, SemanticRecord]) -> None:
        super().__init__(None)
        self._records = records
        self.queried: list[str] = []

    async def query(self, query_input: Any, *, top_k: int = 5) -> SemanticRecord:
        name = str(query_input)
        self.queried.append(name)
        if name not in self._records:
            raise KeyError(f"Service not found in semantic memory: {name!r}")
        return self._records[name]


class _FailingSemantic(SemanticMemory):
    """SemanticMemory whose database fails mid-query."""

    async def query(self, query_input: Any, *, top_k: int = 5) -> SemanticRecord:
        raise SentinelMemoryError("database operation failed: ConnectionDoesNotExistError")


_GATEWAY = SemanticRecord(
    service=ServiceMetadata(
        name="api-gateway",
        team="team-platform",
        tier=ServiceTier.CRITICAL,
        oncall_channel="#oncall-platform",
        repo_url="https://github.com/acme/api-gateway",
        description="Edge routing",
        dependencies=["auth-service", "payment-service"],
    ),
    runbooks=[
        Runbook(
            id="api-gateway-runbook",
            service_name="api-gateway",
            failure_class="general",
            steps=["Check recent deploys", "Identify the bad commit", "Open a revert PR"],
        )
    ],
)

_LEAF = SemanticRecord(
    service=ServiceMetadata(name="user-service", team="team-users", tier=ServiceTier.STANDARD),
    runbooks=[],
)


def _lookup(name: str) -> ServiceLookupInput:
    return ServiceLookupInput(service_name=name)


async def _invoke(tool: FunctionTool, arguments: dict[str, Any]) -> str:
    """Call the tool the way the Runner does: JSON arguments through on_invoke_tool.

    Returns the text the model reads back — compact JSON, never a Python repr.
    """
    raw = json.dumps(arguments)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="call-1", tool_arguments=raw)
    text = await tool.on_invoke_tool(ctx, raw)
    assert isinstance(text, str), text
    return text


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def mem() -> _InMemorySemantic:
    return _InMemorySemantic({"api-gateway": _GATEWAY, "user-service": _LEAF})


# ── make_service_lookup_tool ──────────────────────────────────────────────────


def test_make_returns_function_tool(mem: SemanticMemory) -> None:
    assert isinstance(make_service_lookup_tool(mem), FunctionTool)


def test_tool_name_is_get_service_metadata(mem: SemanticMemory) -> None:
    assert make_service_lookup_tool(mem).name == "get_service_metadata"


def test_tool_description_is_the_docstring(mem: SemanticMemory) -> None:
    description = make_service_lookup_tool(mem).description
    assert description.startswith("Get ownership, tier, on-call channel, dependencies")
    assert "not_found" in description
    assert "unavailable" in description


def test_tool_input_is_service_lookup_input(mem: SemanticMemory) -> None:
    schema = make_service_lookup_tool(mem).params_json_schema
    assert schema["required"] == ["request"]
    assert schema["$defs"]["ServiceLookupInput"]["required"] == ["service_name"]


def test_tool_params_schema_no_memory_leak(mem: SemanticMemory) -> None:
    """The injected 'memory' dep must NOT appear in the LLM-visible schema."""
    assert "memory" not in str(make_service_lookup_tool(mem).params_json_schema)


def test_service_lookup_input_requires_service_name() -> None:
    with pytest.raises(ValidationError):
        ServiceLookupInput.model_validate({})


# ── _lookup_service ───────────────────────────────────────────────────────────


async def test_known_service_returns_metadata_and_runbook(mem: SemanticMemory) -> None:
    result = await _lookup_service(mem, _lookup("api-gateway"))
    assert result == _GATEWAY
    assert isinstance(result, SemanticRecord)
    assert result.service.tier is ServiceTier.CRITICAL
    assert result.service.dependencies == ["auth-service", "payment-service"]
    assert result.runbooks[0].steps[2] == "Open a revert PR"


async def test_service_without_runbook_returns_empty_runbooks(mem: SemanticMemory) -> None:
    result = await _lookup_service(mem, _lookup("user-service"))
    assert isinstance(result, SemanticRecord)
    assert result.runbooks == []
    assert result.service.oncall_channel is None


async def test_service_name_is_stripped(mem: _InMemorySemantic) -> None:
    result = await _lookup_service(mem, _lookup("  api-gateway \n"))
    assert result == _GATEWAY
    assert mem.queried == ["api-gateway"]


async def test_unknown_service_returns_not_found(mem: SemanticMemory) -> None:
    # Tools return a structured failure, never raise.
    result = await _lookup_service(mem, _lookup("nonexistent-service"))
    assert result == ToolFailure(
        tool="get_service_metadata",
        kind="not_found",
        message="service 'nonexistent-service' is not in the service map",
    )


async def test_blank_service_name_is_bad_input(mem: _InMemorySemantic) -> None:
    result = await _lookup_service(mem, _lookup("   "))
    assert isinstance(result, ToolFailure)
    assert result.kind == "bad_input"
    assert mem.queried == []


async def test_unconfigured_memory_reports_unavailable() -> None:
    """No DATABASE_URL (pool None): unavailable, not a crash and not "not found"."""
    result = await _lookup_service(SemanticMemory(None), _lookup("api-gateway"))
    assert isinstance(result, ToolFailure)
    assert result.tool == "get_service_metadata"
    assert result.kind == "unavailable"
    assert "no database configured" in result.message


async def test_database_failure_reports_unavailable() -> None:
    result = await _lookup_service(_FailingSemantic(None), _lookup("api-gateway"))
    assert isinstance(result, ToolFailure)
    assert result.kind == "unavailable"
    assert "ConnectionDoesNotExistError" in result.message


# ── Runbook caps ──────────────────────────────────────────────────────────────


def _with_steps(*runbook_steps: list[str]) -> SemanticRecord:
    return SemanticRecord(
        service=_LEAF.service,
        runbooks=[
            Runbook(
                id=f"user-service-runbook-{i}",
                service_name="user-service",
                failure_class="general",
                steps=steps,
            )
            for i, steps in enumerate(runbook_steps)
        ],
    )


async def _looked_up(record: SemanticRecord) -> SemanticRecord:
    mem = _InMemorySemantic({"user-service": record})
    result = await _lookup_service(mem, _lookup("user-service"))
    assert isinstance(result, SemanticRecord), result
    return result


async def test_runbook_is_cut_to_10_steps() -> None:
    steps = [f"step {i}" for i in range(25)]
    [runbook] = (await _looked_up(_with_steps(steps))).runbooks
    assert runbook.steps == steps[:10]
    assert runbook.truncated is True


async def test_runbook_is_cut_to_2000_characters() -> None:
    steps = ["a" * 900, "b" * 900, "c" * 900, "d" * 10]
    [runbook] = (await _looked_up(_with_steps(steps))).runbooks
    assert runbook.steps == ["a" * 900, "b" * 900, "c" * 200]
    assert sum(len(step) for step in runbook.steps) == 2000
    assert runbook.truncated is True


async def test_caps_are_shared_by_all_runbooks() -> None:
    record = await _looked_up(_with_steps(["a" * 1500], ["b" * 1000, "c"]))
    first, second = record.runbooks
    assert (first.steps, first.truncated) == (["a" * 1500], False)
    assert (second.steps, second.truncated) == (["b" * 500], True)


async def test_a_runbook_within_the_caps_is_whole_and_unflagged(mem: SemanticMemory) -> None:
    result = await _lookup_service(mem, _lookup("api-gateway"))
    assert result == _GATEWAY
    assert isinstance(result, SemanticRecord)
    assert result.runbooks[0].truncated is False


async def test_the_truncated_flag_reaches_the_model() -> None:
    mem = _InMemorySemantic({"user-service": _with_steps([f"s{i}" for i in range(11)])})
    text = await _invoke(
        make_service_lookup_tool(mem), {"request": {"service_name": "user-service"}}
    )
    data = json.loads(text)
    assert data["runbooks"][0]["truncated"] is True
    assert len(data["runbooks"][0]["steps"]) == 10


# ── The FunctionTool as the Runner calls it ───────────────────────────────────


async def test_invoked_tool_returns_the_record(mem: SemanticMemory) -> None:
    tool = make_service_lookup_tool(mem)
    result = await _invoke(tool, {"request": {"service_name": "api-gateway"}})
    assert SemanticRecord.model_validate_json(result) == _GATEWAY
    assert result == _GATEWAY.model_dump_json(exclude_none=True)


async def test_invoked_tool_returns_failure_for_unknown_service(mem: SemanticMemory) -> None:
    tool = make_service_lookup_tool(mem)
    result = await _invoke(tool, {"request": {"service_name": "ghost"}})
    data = json.loads(result)
    # The model reads this JSON: "error" marks it a failure, kind says how to read it.
    assert data["error"] is True
    assert (data["tool"], data["kind"]) == ("get_service_metadata", "not_found")
    assert ToolFailure.model_validate(data).message == data["message"]
