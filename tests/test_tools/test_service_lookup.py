"""Tests for sentinel.tools.service_lookup — get_service_metadata tool.

Unit tests: no database. The tool is exercised against an in-memory stand-in for
SemanticMemory, and against a real SemanticMemory with no pool for the "memory
unavailable" path. Lookups against the services table are covered by
tests/test_memory/test_semantic.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from agents import FunctionTool

from sentinel.exceptions import SentinelMemoryError
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.memory import Runbook, SemanticRecord
from sentinel.models.service import ServiceMetadata, ServiceTier
from sentinel.tools.service_lookup import (
    _build_service_response,
    _format_record,
    make_service_lookup_tool,
)


class _InMemorySemantic(SemanticMemory):
    """SemanticMemory stand-in: records in a dict. Only query (the tool's call) is overridden."""

    def __init__(self, records: dict[str, SemanticRecord]) -> None:
        super().__init__(None)
        self._records = records

    async def query(self, query_input: Any, *, top_k: int = 5) -> SemanticRecord:
        name = str(query_input)
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

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def mem() -> SemanticMemory:
    return _InMemorySemantic({"api-gateway": _GATEWAY, "user-service": _LEAF})


# ── make_service_lookup_tool ──────────────────────────────────────────────────


def test_make_returns_function_tool(mem: SemanticMemory) -> None:
    tool = make_service_lookup_tool(mem)
    assert isinstance(tool, FunctionTool)


def test_tool_name_is_get_service_metadata(mem: SemanticMemory) -> None:
    tool = make_service_lookup_tool(mem)
    assert tool.name == "get_service_metadata"


def test_tool_has_description(mem: SemanticMemory) -> None:
    tool = make_service_lookup_tool(mem)
    assert tool.description and len(tool.description) > 10


def test_tool_params_schema_has_service_name(mem: SemanticMemory) -> None:
    tool = make_service_lookup_tool(mem)
    schema_str = str(tool.params_json_schema)
    assert "service_name" in schema_str


def test_tool_params_schema_no_memory_leak(mem: SemanticMemory) -> None:
    """The injected 'memory' dep must NOT appear in the LLM-visible schema."""
    tool = make_service_lookup_tool(mem)
    schema_str = str(tool.params_json_schema)
    assert "memory" not in schema_str


# ── _build_service_response ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_known_service_response_has_every_field(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "api-gateway")
    for expected in (
        "name: api-gateway",
        "team: team-platform",
        "tier: critical",
        "oncall_channel: #oncall-platform",
        "repo_url: https://github.com/acme/api-gateway",
        "description: Edge routing",
        "dependencies: auth-service, payment-service",
        "runbooks (1):",
        "[general] Check recent deploys → Identify the bad commit → Open a revert PR",
    ):
        assert expected in result


@pytest.mark.asyncio
async def test_leaf_service_shows_none_for_missing_fields(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "user-service")
    assert "dependencies: none" in result
    assert "oncall_channel: none" in result
    assert "repo_url: unknown" in result
    assert "runbooks: none" in result


@pytest.mark.asyncio
async def test_unknown_service_returns_not_found(mem: SemanticMemory) -> None:
    # Tools must return errors as strings, never raise.
    result = await _build_service_response(mem, "nonexistent-service")
    assert result == "ERROR: Service 'nonexistent-service' not found in the service map."


@pytest.mark.asyncio
async def test_unconfigured_memory_reports_unavailable() -> None:
    """No DATABASE_URL (pool None): a clear tool result, not a crash or "not found"."""
    result = await _build_service_response(SemanticMemory(None), "api-gateway")
    assert result.startswith("ERROR: memory unavailable")
    assert "no database configured" in result
    assert "not found" not in result


@pytest.mark.asyncio
async def test_database_failure_reports_unavailable() -> None:
    result = await _build_service_response(_FailingSemantic(None), "api-gateway")
    assert result.startswith("ERROR: memory unavailable")
    assert "ConnectionDoesNotExistError" in result


# ── _format_record (unit tests with synthetic data) ───────────────────────────


def _make_record(
    name: str = "test-service",
    team: str = "team-test",
    tier: ServiceTier = ServiceTier.STANDARD,
    oncall_channel: str | None = "#oncall-test",
    repo_url: str | None = "https://github.com/acme/test",
    description: str = "A test service",
    dependencies: list[str] | None = None,
    runbooks: list[Runbook] | None = None,
) -> SemanticRecord:
    svc = ServiceMetadata(
        name=name,
        team=team,
        tier=tier,
        oncall_channel=oncall_channel,
        repo_url=repo_url,
        description=description,
        dependencies=dependencies or [],
    )
    return SemanticRecord(service=svc, runbooks=runbooks or [])


def test_format_record_contains_name() -> None:
    record = _make_record(name="my-service")
    result = _format_record(record)
    assert "my-service" in result


def test_format_record_contains_team() -> None:
    record = _make_record(team="team-alpha")
    result = _format_record(record)
    assert "team-alpha" in result


def test_format_record_contains_tier() -> None:
    record = _make_record(tier=ServiceTier.CRITICAL)
    result = _format_record(record)
    assert "critical" in result


def test_format_record_lists_dependencies() -> None:
    record = _make_record(dependencies=["svc-a", "svc-b"])
    result = _format_record(record)
    assert "svc-a" in result
    assert "svc-b" in result


def test_format_record_no_dependencies_shows_none() -> None:
    record = _make_record(dependencies=[])
    result = _format_record(record)
    assert "none" in result


def test_format_record_no_runbooks_shows_none() -> None:
    record = _make_record(runbooks=[])
    result = _format_record(record)
    assert "none" in result


def test_format_record_runbooks_show_failure_class() -> None:
    rb = Runbook(
        id="rb-001",
        service_name="test-service",
        failure_class="bad_deploy",
        steps=["Check logs", "Identify commit", "Rollback"],
    )
    record = _make_record(runbooks=[rb])
    result = _format_record(record)
    assert "bad_deploy" in result


def test_format_record_runbook_steps_preview_up_to_3() -> None:
    rb = Runbook(
        id="rb-002",
        service_name="test-service",
        failure_class="db_pool",
        steps=["Step 1", "Step 2", "Step 3", "Step 4", "Step 5"],
    )
    record = _make_record(runbooks=[rb])
    result = _format_record(record)
    assert "Step 1" in result
    assert "Step 2" in result
    assert "Step 3" in result
    # Step 4 and 5 should be hidden in the "N steps total" note
    assert "5 steps total" in result


def test_format_record_runbook_3_steps_no_truncation() -> None:
    rb = Runbook(
        id="rb-003",
        service_name="test-service",
        failure_class="db_pool",
        steps=["Step A", "Step B", "Step C"],
    )
    record = _make_record(runbooks=[rb])
    result = _format_record(record)
    assert "Step A" in result
    assert "Step B" in result
    assert "Step C" in result
    assert "steps total" not in result


def test_format_record_multiple_runbooks_all_present() -> None:
    rbs = [
        Runbook(id="rb-1", service_name="svc", failure_class="bad_deploy", steps=["X"]),
        Runbook(id="rb-2", service_name="svc", failure_class="memory_leak", steps=["Y"]),
    ]
    record = _make_record(runbooks=rbs)
    result = _format_record(record)
    assert "bad_deploy" in result
    assert "memory_leak" in result


def test_format_record_missing_oncall_shows_none() -> None:
    record = _make_record(oncall_channel=None)
    result = _format_record(record)
    assert "none" in result


def test_format_record_missing_repo_shows_unknown() -> None:
    record = _make_record(repo_url=None)
    result = _format_record(record)
    assert "unknown" in result


def test_format_record_returns_multiline_string() -> None:
    record = _make_record()
    result = _format_record(record)
    assert "\n" in result


def test_format_record_runbook_count_in_header() -> None:
    rbs = [
        Runbook(id="rb-1", service_name="svc", failure_class="bad_deploy", steps=["X"]),
        Runbook(id="rb-2", service_name="svc", failure_class="db_pool", steps=["Y"]),
        Runbook(id="rb-3", service_name="svc", failure_class="memory_leak", steps=["Z"]),
    ]
    record = _make_record(runbooks=rbs)
    result = _format_record(record)
    assert "3" in result  # runbook count appears in "runbooks (3):" header
