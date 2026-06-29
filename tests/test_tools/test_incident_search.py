"""Tests for sentinel.tools.incident_search — search_past_incidents tool."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from agents import FunctionTool

from sentinel.infra.db import create_tables
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.incident import Incident, IncidentStatus, Severity
from sentinel.models.memory import EpisodicRecord, MemoryQueryResult
from sentinel.tools.incident_search import (
    _format_results,
    _search_past_incidents,
    make_incident_search_tool,
)

DIMS = 4


class _KeywordFakeModel:
    """Keyword-based fake embedding model for deterministic similarity testing."""

    def encode(
        self,
        texts: list[str],
        *,
        convert_to_numpy: bool = True,
        show_progress_bar: bool = False,
    ) -> np.ndarray:
        result = []
        for text in texts:
            vec = np.zeros(DIMS, dtype=np.float32)
            t = text.lower()
            if any(kw in t for kw in ("connection", "pool", "database")):
                vec[0] = 1.0
            elif any(kw in t for kw in ("memory", "oom", "leak")):
                vec[1] = 1.0
            elif any(kw in t for kw in ("deploy", "null", "pointer")):
                vec[2] = 1.0
            else:
                vec[3] = 1.0
            result.append(vec)
        return np.array(result, dtype=np.float32)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_incident(
    service: str,
    metric: str,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
) -> Incident:
    alert = AlertPayload(
        source=AlertSource.DATADOG,
        service=service,
        metric=metric,
        threshold=80.0,
        current_value=95.0,
        severity=severity,
    )
    inc = Incident(alert=alert)
    inc.affected_service = service
    inc.severity = Severity.P1
    inc.status = IncidentStatus.RESOLVED
    return inc


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test_incident_search.db"
    await create_tables(path)
    return path


@pytest.fixture()
def client() -> EmbeddingClient:
    return EmbeddingClient(_KeywordFakeModel())


@pytest.fixture()
def mem(db_path: Path, client: EmbeddingClient) -> EpisodicMemory:
    return EpisodicMemory(db_path, client)


@pytest.fixture()
async def seeded_mem(mem: EpisodicMemory) -> EpisodicMemory:
    """Memory with 3 stored incidents in different clusters."""
    inc_db = _make_incident("payment-service", "db_connection_pool")
    inc_deploy = _make_incident("api-gateway", "null_pointer_deploy")
    inc_mem = _make_incident("analytics-pipeline", "memory_leak_oom")
    await mem.store_incident(inc_db)
    await mem.store_incident(inc_deploy)
    await mem.store_incident(inc_mem)
    return mem


# ── make_incident_search_tool ────────────────────────────────────────────────


def test_make_returns_function_tool(mem: EpisodicMemory) -> None:
    tool = make_incident_search_tool(mem)
    assert isinstance(tool, FunctionTool)


def test_tool_name_is_search_past_incidents(mem: EpisodicMemory) -> None:
    tool = make_incident_search_tool(mem)
    assert tool.name == "search_past_incidents"


def test_tool_has_description(mem: EpisodicMemory) -> None:
    tool = make_incident_search_tool(mem)
    assert tool.description and len(tool.description) > 10


def test_tool_schema_has_symptom_query(mem: EpisodicMemory) -> None:
    tool = make_incident_search_tool(mem)
    assert "symptom_query" in str(tool.params_json_schema)


def test_tool_schema_has_top_k(mem: EpisodicMemory) -> None:
    tool = make_incident_search_tool(mem)
    assert "top_k" in str(tool.params_json_schema)


def test_tool_memory_not_in_schema(mem: EpisodicMemory) -> None:
    """Injected memory dep must NOT appear in the LLM-visible schema."""
    tool = make_incident_search_tool(mem)
    assert "memory" not in str(tool.params_json_schema)


# ── _search_past_incidents — error handling ──────────────────────────────────


@pytest.mark.asyncio
async def test_none_memory_returns_error() -> None:
    result = await _search_past_incidents(None, "connection pool", 3)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_none_memory_does_not_raise() -> None:
    result = await _search_past_incidents(None, "connection pool", 3)
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_empty_query_returns_error(mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(mem, "", 3)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_whitespace_query_returns_error(mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(mem, "   ", 3)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_zero_top_k_returns_error(mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(mem, "connection pool", 0)
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_negative_top_k_returns_error(mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(mem, "connection pool", -1)
    assert "ERROR" in result


# ── _search_past_incidents — successful retrieval ────────────────────────────


@pytest.mark.asyncio
async def test_returns_string(seeded_mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(seeded_mem, "connection pool", 3)
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_empty_db_returns_no_incidents(mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(mem, "connection pool", 3)
    assert "No similar past incidents found" in result


@pytest.mark.asyncio
async def test_matching_incident_appears_in_results(
    seeded_mem: EpisodicMemory,
) -> None:
    result = await _search_past_incidents(seeded_mem, "database connection pool", 3)
    assert "payment-service" in result


@pytest.mark.asyncio
async def test_top_result_is_most_similar(seeded_mem: EpisodicMemory) -> None:
    """Query for 'connection pool' should rank the DB incident first."""
    result = await _search_past_incidents(seeded_mem, "database connection pool", 3)
    payment_pos = result.find("payment-service")
    api_pos = result.find("api-gateway")
    analytics_pos = result.find("analytics-pipeline")
    assert payment_pos < api_pos or api_pos == -1
    assert payment_pos < analytics_pos or analytics_pos == -1


@pytest.mark.asyncio
async def test_top_k_limits_results(seeded_mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(seeded_mem, "connection pool", 1)
    assert "Found 1" in result


@pytest.mark.asyncio
async def test_result_contains_found_count(seeded_mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(seeded_mem, "connection pool", 5)
    assert "Found" in result


@pytest.mark.asyncio
async def test_result_contains_severity(seeded_mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(seeded_mem, "connection pool", 3)
    assert "severity" in result


@pytest.mark.asyncio
async def test_result_contains_root_cause(seeded_mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(seeded_mem, "connection pool", 3)
    assert "root_cause" in result


@pytest.mark.asyncio
async def test_result_contains_resolution(seeded_mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(seeded_mem, "connection pool", 3)
    assert "resolution" in result


@pytest.mark.asyncio
async def test_result_contains_similarity_score(seeded_mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(seeded_mem, "connection pool", 3)
    assert "similarity" in result


@pytest.mark.asyncio
async def test_result_contains_mttr(seeded_mem: EpisodicMemory) -> None:
    result = await _search_past_incidents(seeded_mem, "connection pool", 3)
    assert "mttr" in result


@pytest.mark.asyncio
async def test_deploy_query_finds_deploy_incident(
    seeded_mem: EpisodicMemory,
) -> None:
    """Query about deploys should find the api-gateway deploy incident."""
    result = await _search_past_incidents(seeded_mem, "null pointer deploy", 3)
    assert "api-gateway" in result


@pytest.mark.asyncio
async def test_memory_query_finds_memory_incident(
    seeded_mem: EpisodicMemory,
) -> None:
    """Query about memory leaks should find the analytics OOM incident."""
    result = await _search_past_incidents(seeded_mem, "memory leak oom", 3)
    assert "analytics-pipeline" in result


# ── _format_results ──────────────────────────────────────────────────────────


def test_format_empty_returns_no_incidents() -> None:
    result = _format_results(MemoryQueryResult())
    assert "No similar past incidents found" in result


def test_format_header_contains_count() -> None:
    record = EpisodicRecord(
        id="inc-001",
        service_name="api-gateway",
        severity="P1",
        symptoms="error spike",
        root_cause="bad deploy",
        resolution="rollback",
    )
    result = _format_results(MemoryQueryResult(records=[record], similarity_scores=[0.95]))
    assert "Found 1" in result


def test_format_contains_incident_id() -> None:
    record = EpisodicRecord(
        id="inc-001",
        service_name="api-gateway",
        severity="P1",
        symptoms="error spike",
        root_cause="bad deploy",
        resolution="rollback",
    )
    result = _format_results(MemoryQueryResult(records=[record], similarity_scores=[0.95]))
    assert "inc-001" in result


def test_format_contains_all_fields() -> None:
    record = EpisodicRecord(
        id="inc-001",
        service_name="api-gateway",
        severity="P1",
        symptoms="error spike",
        root_cause="bad deploy",
        resolution="rollback",
        mttr_seconds=300,
    )
    result = _format_results(MemoryQueryResult(records=[record], similarity_scores=[0.95]))
    assert "service:" in result
    assert "severity:" in result
    assert "symptoms:" in result
    assert "root_cause:" in result
    assert "resolution:" in result
    assert "mttr:" in result
    assert "similarity:" in result


def test_format_similarity_score_formatted() -> None:
    record = EpisodicRecord(
        id="inc-001",
        service_name="api-gateway",
        severity="P1",
        symptoms="error spike",
        root_cause="bad deploy",
        resolution="rollback",
    )
    result = _format_results(MemoryQueryResult(records=[record], similarity_scores=[0.9512]))
    assert "0.951" in result


def test_format_mttr_unknown_when_none() -> None:
    record = EpisodicRecord(
        id="inc-001",
        service_name="api-gateway",
        severity="P1",
        symptoms="error spike",
        root_cause="bad deploy",
        resolution="rollback",
        mttr_seconds=None,
    )
    result = _format_results(MemoryQueryResult(records=[record], similarity_scores=[0.9]))
    assert "unknown" in result


def test_format_mttr_shown_when_present() -> None:
    record = EpisodicRecord(
        id="inc-001",
        service_name="api-gateway",
        severity="P1",
        symptoms="error spike",
        root_cause="bad deploy",
        resolution="rollback",
        mttr_seconds=600,
    )
    result = _format_results(MemoryQueryResult(records=[record], similarity_scores=[0.9]))
    assert "600s" in result


def test_format_multiple_records_all_shown() -> None:
    r1 = EpisodicRecord(
        id="inc-001",
        service_name="api-gateway",
        severity="P1",
        symptoms="error spike",
        root_cause="bad deploy",
        resolution="rollback",
    )
    r2 = EpisodicRecord(
        id="inc-002",
        service_name="payment-service",
        severity="P2",
        symptoms="timeout",
        root_cause="pool exhaustion",
        resolution="scale up",
    )
    result = _format_results(MemoryQueryResult(records=[r1, r2], similarity_scores=[0.95, 0.72]))
    assert "inc-001" in result
    assert "inc-002" in result
    assert "Found 2" in result
