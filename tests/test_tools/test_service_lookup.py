"""Tests for sentinel.tools.service_lookup — get_service_metadata tool."""

from __future__ import annotations

from pathlib import Path

import pytest
from agents import FunctionTool

from data.seed import seed
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.memory import Runbook, SemanticRecord
from sentinel.models.service import ServiceMetadata, ServiceTier
from sentinel.tools.service_lookup import (
    _build_service_response,
    _format_record,
    make_service_lookup_tool,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test_service_lookup.db"
    await seed(path, verbose=False)
    return path


@pytest.fixture()
def mem(db_path: Path) -> SemanticMemory:
    return SemanticMemory(db_path)


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


# ── _build_service_response (integration via seeded DB) ──────────────────────


@pytest.mark.asyncio
async def test_known_service_returns_string(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "api-gateway")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_response_contains_service_name(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "api-gateway")
    assert "api-gateway" in result


@pytest.mark.asyncio
async def test_response_contains_team(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "api-gateway")
    assert "team-platform" in result


@pytest.mark.asyncio
async def test_response_contains_tier(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "api-gateway")
    assert "critical" in result


@pytest.mark.asyncio
async def test_response_contains_oncall_channel(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "api-gateway")
    assert "oncall" in result.lower() or "#oncall" in result


@pytest.mark.asyncio
async def test_response_contains_dependencies(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "api-gateway")
    # api-gateway depends on auth-service, payment-service, order-service
    assert "auth-service" in result or "payment-service" in result


@pytest.mark.asyncio
async def test_response_leaf_service_has_none_dependencies(mem: SemanticMemory) -> None:
    # user-service has no dependencies
    result = await _build_service_response(mem, "user-service")
    assert "none" in result


@pytest.mark.asyncio
async def test_response_contains_runbooks_section(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "api-gateway")
    assert "runbook" in result.lower()


@pytest.mark.asyncio
async def test_response_runbooks_include_failure_class(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "api-gateway")
    # api-gateway has a bad_deploy runbook
    assert "bad_deploy" in result


@pytest.mark.asyncio
async def test_response_runbooks_include_step_preview(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "api-gateway")
    # Runbook steps should appear as a preview
    # Steps are connected with " → " in the format
    assert "→" in result


@pytest.mark.asyncio
async def test_all_8_services_return_valid_response(mem: SemanticMemory) -> None:
    services = [
        "api-gateway",
        "user-service",
        "auth-service",
        "payment-service",
        "order-service",
        "notification-service",
        "analytics-pipeline",
        "cdn-proxy",
    ]
    for svc in services:
        result = await _build_service_response(mem, svc)
        assert svc in result, f"Service name missing from response for {svc}"


@pytest.mark.asyncio
async def test_unknown_service_returns_error_string(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "nonexistent-service")
    assert "ERROR" in result or "not found" in result.lower()


@pytest.mark.asyncio
async def test_unknown_service_does_not_raise(mem: SemanticMemory) -> None:
    # Tools must return errors as strings, never raise
    result = await _build_service_response(mem, "totally-made-up-service")
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_payment_service_has_db_pool_runbook(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "payment-service")
    assert "db_pool" in result


@pytest.mark.asyncio
async def test_analytics_pipeline_has_memory_leak_runbook(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "analytics-pipeline")
    assert "memory_leak" in result


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
