"""Tests for sentinel.memory.episodic.EpisodicMemory."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from sentinel.infra.db import create_tables
from sentinel.memory.base import MemoryStore
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.incident import Incident, IncidentStatus, Severity
from sentinel.models.memory import EpisodicRecord, MemoryQueryResult

DIMS = 4


class _KeywordFakeModel:
    """Keyword-based fake embedding model for deterministic similarity testing.

    Assigns unit vectors based on topic keywords in the input text:
      dim 0 → database / connection / pool topics
      dim 1 → memory / oom / leak topics
      dim 2 → deploy / null / pointer topics
      dim 3 → all other topics

    Two texts in the same cluster have cosine similarity 1.0.
    Two texts in different clusters have cosine similarity 0.0.
    This makes ranking assertions precise and deterministic.
    """

    def __init__(self) -> None:
        self.encode_calls: int = 0

    def encode(
        self,
        texts: list[str],
        *,
        convert_to_numpy: bool = True,
        show_progress_bar: bool = False,
    ) -> np.ndarray:
        self.encode_calls += 1
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


# ── Fixtures ──────────────────────────────────────────────────────────────────


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


@pytest.fixture()
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test_episodic.db"
    await create_tables(path)
    return path


@pytest.fixture()
def fake_model() -> _KeywordFakeModel:
    return _KeywordFakeModel()


@pytest.fixture()
def client(fake_model: _KeywordFakeModel) -> EmbeddingClient:
    return EmbeddingClient(fake_model)


@pytest.fixture()
def mem(db_path: Path, client: EmbeddingClient) -> EpisodicMemory:
    return EpisodicMemory(db_path, client)


# ── Protocol compliance ───────────────────────────────────────────────────────


def test_episodic_memory_satisfies_protocol() -> None:
    instance = EpisodicMemory.__new__(EpisodicMemory)
    assert isinstance(instance, MemoryStore)


# ── store_incident / get_incident ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_and_retrieve_incident(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert result.id == inc.id


@pytest.mark.asyncio
async def test_stored_record_service_name(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert result.service_name == "payment-service"


@pytest.mark.asyncio
async def test_stored_record_severity(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert result.severity == "P1"


@pytest.mark.asyncio
async def test_stored_record_has_embedding(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert len(result.embedding) == DIMS


@pytest.mark.asyncio
async def test_stored_record_symptoms_non_empty(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert result.symptoms != ""


@pytest.mark.asyncio
async def test_stored_record_symptoms_contains_service(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert "payment-service" in result.symptoms


@pytest.mark.asyncio
async def test_get_incident_not_found_returns_none(mem: EpisodicMemory) -> None:
    result = await mem.get_incident("inc-nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_store_incident_upsert_on_duplicate_id(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    await mem.store_incident(inc)  # second store — should upsert, not raise
    result = await mem.get_incident(inc.id)
    assert result is not None


@pytest.mark.asyncio
async def test_store_mttr_seconds_when_resolved(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    inc.resolved_at = inc.created_at + timedelta(seconds=300)
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert result.mttr_seconds == 300


@pytest.mark.asyncio
async def test_store_mttr_seconds_none_when_unresolved(mem: EpisodicMemory) -> None:
    inc = _make_incident("api-gateway", "error_rate")
    inc.resolved_at = None
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert result.mttr_seconds is None


@pytest.mark.asyncio
async def test_stored_record_root_cause_from_log_analysis(mem: EpisodicMemory) -> None:
    inc = _make_incident("api-gateway", "error_rate")
    inc.log_analysis = {"hypothesis": "null pointer in auth middleware"}
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert "null pointer" in result.root_cause


@pytest.mark.asyncio
async def test_stored_record_resolution_from_remediation_plan(mem: EpisodicMemory) -> None:
    inc = _make_incident("api-gateway", "error_rate")
    inc.remediation_plan = {"action_type": "rollback", "justification": "revert to stable"}
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert result.resolution == "revert to stable"


@pytest.mark.asyncio
async def test_stored_record_raw_timeline_present(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None
    assert result.raw_timeline is not None
    assert result.raw_timeline["id"] == inc.id


# ── search_similar ranking ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_similar_ranks_db_incident_first(mem: EpisodicMemory) -> None:
    """The core episodic memory test: store 3 incidents, verify ranking.

    db_inc metric contains 'pool' → dim-0 embedding.
    mem_inc metric contains 'memory' → dim-1 embedding.
    other_inc metric 'error_rate' → dim-3 embedding (fallback).

    Query contains 'connection' and 'pool' → dim-0 → cosine sim 1.0 with
    db_inc, 0.0 with mem_inc and other_inc. db_inc must rank first.
    """
    db_inc = _make_incident("payment-service", "db_connection_pool")
    mem_inc = _make_incident("analytics-pipeline", "memory_usage_percent", AlertSeverity.HIGH)
    other_inc = _make_incident("api-gateway", "error_rate")

    await mem.store_incident(db_inc)
    await mem.store_incident(mem_inc)
    await mem.store_incident(other_inc)

    results = await mem.search_similar("database connection pool exhausted", top_k=3)

    assert len(results) >= 1
    assert results[0].id == db_inc.id


@pytest.mark.asyncio
async def test_search_similar_ranks_memory_incident_first(mem: EpisodicMemory) -> None:
    db_inc = _make_incident("payment-service", "db_connection_pool")
    mem_inc = _make_incident("analytics-pipeline", "memory_usage_percent")
    await mem.store_incident(db_inc)
    await mem.store_incident(mem_inc)

    results = await mem.search_similar("memory leak oom heap usage high", top_k=2)

    assert len(results) >= 1
    assert results[0].id == mem_inc.id


@pytest.mark.asyncio
async def test_search_similar_returns_episodic_records(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    results = await mem.search_similar("connection pool exhausted")
    assert isinstance(results, list)
    assert all(isinstance(r, EpisodicRecord) for r in results)


@pytest.mark.asyncio
async def test_search_similar_empty_db_returns_empty(mem: EpisodicMemory) -> None:
    results = await mem.search_similar("any symptoms here")
    assert results == []


@pytest.mark.asyncio
async def test_search_similar_top_k_limits_results(mem: EpisodicMemory) -> None:
    for i in range(5):
        inc = _make_incident("payment-service", f"db_connection_pool_{i}")
        await mem.store_incident(inc)
    results = await mem.search_similar("database connection pool exhausted", top_k=2)
    assert len(results) <= 2


@pytest.mark.asyncio
async def test_search_similar_returns_all_when_fewer_than_top_k(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    results = await mem.search_similar("connection pool", top_k=10)
    assert len(results) == 1


# ── delete ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_record(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    await mem.delete(inc.id)
    result = await mem.get_incident(inc.id)
    assert result is None


@pytest.mark.asyncio
async def test_delete_nonexistent_is_noop(mem: EpisodicMemory) -> None:
    await mem.delete("inc-nonexistent")  # must not raise


@pytest.mark.asyncio
async def test_delete_does_not_affect_other_records(mem: EpisodicMemory) -> None:
    inc_a = _make_incident("payment-service", "db_connection_pool")
    inc_b = _make_incident("analytics-pipeline", "memory_usage_percent")
    await mem.store_incident(inc_a)
    await mem.store_incident(inc_b)
    await mem.delete(inc_a.id)
    result_b = await mem.get_incident(inc_b.id)
    assert result_b is not None
    assert result_b.id == inc_b.id


@pytest.mark.asyncio
async def test_delete_removes_from_search_results(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    await mem.delete(inc.id)
    results = await mem.search_similar("database connection pool exhausted")
    assert all(r.id != inc.id for r in results)


# ── MemoryStore protocol methods ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_protocol_delegates_to_store_incident(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store(inc)
    result = await mem.get_incident(inc.id)
    assert result is not None


@pytest.mark.asyncio
async def test_store_protocol_raises_type_error_for_non_incident(mem: EpisodicMemory) -> None:
    with pytest.raises(TypeError, match="Incident"):
        await mem.store({"id": "not-an-incident"})


@pytest.mark.asyncio
async def test_get_protocol_returns_episodic_record(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.get(inc.id)
    assert isinstance(result, EpisodicRecord)
    assert result.id == inc.id


@pytest.mark.asyncio
async def test_get_protocol_returns_none_for_missing(mem: EpisodicMemory) -> None:
    result = await mem.get("inc-missing")
    assert result is None


@pytest.mark.asyncio
async def test_query_returns_memory_query_result(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.query("database connection pool")
    assert isinstance(result, MemoryQueryResult)


@pytest.mark.asyncio
async def test_query_records_and_scores_parallel(mem: EpisodicMemory) -> None:
    for i in range(3):
        inc = _make_incident("payment-service", f"db_connection_pool_{i}")
        await mem.store_incident(inc)
    result = await mem.query("database connection pool", top_k=3)
    assert len(result.records) == len(result.similarity_scores)


@pytest.mark.asyncio
async def test_query_scores_between_zero_and_one(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.query("database connection pool")
    assert all(0.0 <= s <= 1.0 for s in result.similarity_scores)


@pytest.mark.asyncio
async def test_query_scores_in_descending_order(mem: EpisodicMemory) -> None:
    db_inc = _make_incident("payment-service", "db_connection_pool")
    mem_inc = _make_incident("analytics-pipeline", "memory_usage_percent")
    await mem.store_incident(db_inc)
    await mem.store_incident(mem_inc)

    result = await mem.query("database connection pool exhausted", top_k=2)
    if len(result.similarity_scores) >= 2:
        assert result.similarity_scores[0] >= result.similarity_scores[1]


@pytest.mark.asyncio
async def test_query_top_incident_score_is_one_for_exact_cluster_match(
    mem: EpisodicMemory,
) -> None:
    """When query and stored incident are in the same keyword cluster, sim = 1.0."""
    db_inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(db_inc)

    result = await mem.query("database connection pool exhausted")
    assert len(result.similarity_scores) == 1
    assert abs(result.similarity_scores[0] - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_query_empty_db_returns_empty_result(mem: EpisodicMemory) -> None:
    result = await mem.query("database connection pool")
    assert result.records == []
    assert result.similarity_scores == []
