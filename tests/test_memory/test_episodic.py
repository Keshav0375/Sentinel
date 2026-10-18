"""Tests for sentinel.memory.episodic.EpisodicMemory — asyncpg + pgvector.

Tests taking ``mem`` run against a real pgvector container (TEST_DATABASE_URL, see
tests/conftest.py): this module migrates one throwaway database to the §5.3 schema,
empties ``incidents`` before each test and drops the database afterwards. They skip
with a reason when TEST_DATABASE_URL is not set (fail in CI).

Embeddings come from fake models (no torch) that emit 384-dim vectors, the width of
``incidents.embedding``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import numpy as np
import pytest
from alembic import command
from alembic.config import Config

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.database import create_pool
from sentinel.memory.base import MemoryStore
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.incident import Incident, IncidentStatus, Severity, TimelineEntry
from sentinel.models.memory import EpisodicRecord, MemoryQueryResult
from sentinel.models.tools import IncidentSearchInput, SimilarIncident, ToolFailure
from sentinel.tools.incident_search import _search_past_incidents

DIMS = 384


class _KeywordFakeModel:
    """Keyword-cluster fake embedding model: deterministic, exact similarities.

    Assigns a 384-dim unit vector by topic keyword:
      dim 0 → database / connection / pool
      dim 1 → memory / oom / leak
      dim 2 → deploy / null / pointer
      dim 3 → anything else

    Same cluster → cosine similarity 1.0; different clusters → 0.0.
    """

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


class _SpreadFakeModel:
    """Fake model whose vectors use every one of the 384 dims, varying per text."""

    def encode(
        self,
        texts: list[str],
        *,
        convert_to_numpy: bool = True,
        show_progress_bar: bool = False,
    ) -> np.ndarray:
        rows = []
        for text in texts:
            seed = sum(text.encode())
            rows.append([((seed + 31 * i) % 97) / 97.0 - 0.5 for i in range(DIMS)])
        return np.array(rows, dtype=np.float32)


def _one_hot(dim: int) -> list[float]:
    vec = [0.0] * DIMS
    vec[dim] = 1.0
    return vec


# ── Database fixtures ─────────────────────────────────────────────────────────


def _with_database(dsn: str, name: str) -> str:
    return urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))


def _execute(dsn: str, sql: str) -> None:
    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_go())


@pytest.fixture(scope="module")
def _module_db() -> Iterator[dict[str, str]]:
    """Holder for this module's throwaway database; dropped when the module finishes."""
    holder: dict[str, str] = {}
    yield holder
    if holder:
        _execute(holder["server"], f'DROP DATABASE IF EXISTS "{holder["name"]}" WITH (FORCE)')


@pytest.fixture
def episodic_dsn(
    pg_dsn: str, _module_db: dict[str, str], alembic_config: Callable[..., Config]
) -> str:
    """A database at migration 001 (the §5.3 schema), with ``incidents`` emptied.

    Created and migrated on first use in this module; 001 alone holds ``incidents``
    and needs no embedding model (002 seeds ``services``).
    """
    if not _module_db:
        name = f"sentinel_episodic_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"]), "001")
    _execute(_module_db["dsn"], "TRUNCATE incidents CASCADE")
    return _module_db["dsn"]


@pytest.fixture
async def pool(episodic_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(episodic_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def client() -> EmbeddingClient:
    return EmbeddingClient(_KeywordFakeModel())


@pytest.fixture
def mem(pool: asyncpg.Pool, client: EmbeddingClient) -> EpisodicMemory:
    return EpisodicMemory(pool, client)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_incident(
    service: str,
    metric: str,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    status: IncidentStatus = IncidentStatus.RESOLVED,
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
    inc.status = status
    return inc


async def _raw_row(pool: asyncpg.Pool, incident_id: str) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM incidents WHERE id = $1", uuid.UUID(incident_id))
    assert row is not None
    return row


# ── Protocol compliance ───────────────────────────────────────────────────────


def test_episodic_memory_satisfies_protocol() -> None:
    instance = EpisodicMemory.__new__(EpisodicMemory)
    assert isinstance(instance, MemoryStore)


# ── No database configured ────────────────────────────────────────────────────


def _calls() -> dict[str, Callable[[EpisodicMemory], Any]]:
    inc = _make_incident("payment-service", "db_connection_pool")
    some_id = str(uuid.uuid4())
    return {
        "store_incident": lambda m: m.store_incident(inc),
        "store": lambda m: m.store(inc),
        "search_similar": lambda m: m.search_similar("connection pool"),
        "search_by_embedding": lambda m: m.search_by_embedding(_one_hot(0)),
        "query": lambda m: m.query("connection pool"),
        "get_incident": lambda m: m.get_incident(some_id),
        "get": lambda m: m.get(some_id),
        "list_incidents": lambda m: m.list_incidents(),
        "delete": lambda m: m.delete(some_id),
    }


@pytest.mark.parametrize("method", list(_calls()))
async def test_every_method_raises_without_a_pool(method: str) -> None:
    """pool=None never yields empty results: every call says memory is unavailable."""
    model = MagicMock()
    no_db = EpisodicMemory(None, EmbeddingClient(model))
    with pytest.raises(SentinelMemoryError, match="memory unavailable"):
        await _calls()[method](no_db)
    model.encode.assert_not_called()


# ── store_incident → incidents row (decision 2026-10-12 mapping) ──────────────


async def test_full_round_trip(pool: asyncpg.Pool) -> None:
    """Every mapped field survives store → get, incl. trajectory and the 384-dim vector."""
    client = EmbeddingClient(_SpreadFakeModel())
    mem = EpisodicMemory(pool, client)
    inc = _make_incident("payment-service", "db_connection_pool")
    inc.created_at = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    inc.resolved_at = inc.created_at + timedelta(seconds=300)
    inc.log_analysis = {"hypothesis": "connection leak in checkout handler"}
    inc.remediation_plan = {"action_type": "rollback", "justification": "revert to stable"}
    inc.timeline = [
        TimelineEntry(agent_name="triage", action="classify", result_summary="P1"),
        TimelineEntry(agent_name="log_analyst", action="fetch_logs", result_summary="leak"),
    ]

    await mem.store_incident(inc)
    record = await mem.get_incident(inc.id)

    assert record is not None
    assert record.id == inc.id
    assert record.service_name == "payment-service"
    assert record.severity == "P1"
    assert record.root_cause == "connection leak in checkout handler"
    assert record.resolution == "revert to stable"
    assert record.mttr_seconds == 300
    assert record.created_at == inc.created_at
    assert "payment-service" in record.symptoms
    assert "connection leak in checkout handler" not in record.symptoms  # agent output
    expected = await client.embed(record.symptoms)
    assert len(record.embedding) == DIMS
    assert record.embedding == pytest.approx(expected, abs=1e-6)
    assert record.raw_timeline is not None
    assert record.raw_timeline["id"] == inc.id
    assert record.raw_timeline["status"] == "resolved"
    assert [e["agent_name"] for e in record.raw_timeline["timeline"]] == [
        "triage",
        "log_analyst",
    ]
    provenance = record.raw_timeline["provenance"]
    assert provenance["root_cause_by"] == "log_analyst"
    assert provenance["resolution_by"] == "unknown"  # no remediation agent in the timeline
    assert provenance["model_generated"] is True


async def test_row_columns_follow_the_schema_mapping(
    mem: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    inc = _make_incident("api-gateway", "error_rate", status=IncidentStatus.ESCALATED)
    inc.resolved_at = inc.created_at + timedelta(seconds=42)
    await mem.store_incident(inc)

    row = await _raw_row(pool, inc.id)
    assert row["id"] == uuid.UUID(inc.id)
    assert row["alert_id"] == inc.alert.alert_id
    assert row["service"] == "api-gateway"
    assert row["severity"] == "P1"
    assert row["status"] == "escalated"
    assert row["resolved_at"] == inc.resolved_at
    assert row["mttr_seconds"] == 42
    trajectory = json.loads(row["trajectory"])
    assert set(trajectory) == {"symptoms", "timeline", "provenance"}
    assert trajectory["symptoms"].startswith("Service api-gateway error_rate")
    # Columns the Phase-1 model cannot supply stay NULL / default.
    for column in (
        "dd_event_id",
        "correlation_id",
        "root_cause_confidence",
        "resolution_type",
        "pr_number",
        "pr_url",
        "eval_score",
        "langfuse_trace_id",
    ):
        assert row[column] is None, column
    assert row["reflexion_loops"] == 0


async def test_falls_back_to_alert_severity_and_service(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool", AlertSeverity.HIGH)
    inc.severity = None
    inc.affected_service = None
    await mem.store_incident(inc)
    record = await mem.get_incident(inc.id)
    assert record is not None
    assert record.severity == AlertSeverity.HIGH.value
    assert record.service_name == "payment-service"


async def test_unresolved_incident_has_no_mttr(mem: EpisodicMemory) -> None:
    inc = _make_incident("api-gateway", "error_rate")
    await mem.store_incident(inc)
    record = await mem.get_incident(inc.id)
    assert record is not None
    assert record.mttr_seconds is None
    assert record.root_cause == "unknown"
    assert record.resolution == "unresolved"


async def test_store_upserts_on_same_id(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool", status=IncidentStatus.TRIAGE)
    await mem.store_incident(inc)
    inc.status = IncidentStatus.RESOLVED
    inc.remediation_plan = {"action_type": "rollback"}
    await mem.store_incident(inc)

    assert [r.id for r in await mem.list_incidents()] == [inc.id]
    assert [r.id for r in await mem.list_incidents(status="resolved")] == [inc.id]
    record = await mem.get_incident(inc.id)
    assert record is not None
    assert record.resolution == "rollback"


async def test_restore_keeps_the_original_created_at(
    mem: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    inc = _make_incident("payment-service", "db_connection_pool", status=IncidentStatus.TRIAGE)
    first = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    inc.created_at = first
    await mem.store_incident(inc)

    # A later re-store carries a different created_at (e.g. a rebuilt Incident).
    inc.created_at = first + timedelta(hours=1)
    inc.resolved_at = first + timedelta(hours=1, seconds=30)
    inc.status = IncidentStatus.RESOLVED
    await mem.store_incident(inc)

    row = await _raw_row(pool, inc.id)
    assert row["created_at"] == first
    assert row["status"] == "resolved"
    # MTTR is measured from the kept created_at, not the re-store's.
    assert row["mttr_seconds"] == 3630


# ── Provenance: stored conclusions are marked as model output ─────────────────


async def test_provenance_names_the_agents_from_the_timeline(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    inc.deploy_correlation = {"evidence": "deploy v2.3 introduced the leak"}
    inc.remediation_plan = {"action_type": "rollback"}
    inc.timeline = [
        TimelineEntry(
            agent_name="incident_orchestrator",
            action="handoff → deploy_correlator_agent",
            result_summary="Transferred control to deploy_correlator_agent",
        ),
        TimelineEntry(agent_name="remediation_agent", action="propose", result_summary="ok"),
    ]

    await mem.store_incident(inc)
    record = await mem.get_incident(inc.id)

    assert record is not None
    assert record.root_cause == "deploy v2.3 introduced the leak"
    assert record.raw_timeline is not None
    provenance = record.raw_timeline["provenance"]
    assert provenance["root_cause_by"] == "deploy_correlator_agent"
    assert provenance["resolution_by"] == "remediation_agent"
    assert provenance["model_generated"] is True
    stored_at = datetime.fromisoformat(provenance["stored_at"])
    assert abs(datetime.now(UTC) - stored_at) < timedelta(minutes=1)


async def test_provenance_is_unknown_without_conclusions(mem: EpisodicMemory) -> None:
    inc = _make_incident("api-gateway", "error_rate")
    await mem.store_incident(inc)
    record = await mem.get_incident(inc.id)

    assert record is not None and record.raw_timeline is not None
    provenance = record.raw_timeline["provenance"]
    assert provenance["root_cause_by"] == "unknown"
    assert provenance["resolution_by"] == "unknown"
    assert provenance["model_generated"] is True


async def test_embedded_symptoms_hold_only_observed_alert_data(
    pool: asyncpg.Pool,
) -> None:
    client = EmbeddingClient(_SpreadFakeModel())
    mem = EpisodicMemory(pool, client)
    inc = _make_incident("payment-service", "db_connection_pool")
    inc.alert.metadata = {
        "title": "Checkout errors above SLO",
        "description": "p99 latency 4s on /checkout",
        "scenario_id": "bad_deploy_01",
    }
    inc.affected_service = "orders-db"  # triage's conclusion
    inc.log_analysis = {"hypothesis": "connection leak in checkout handler"}
    inc.deploy_correlation = {"evidence": "deploy v2.3"}
    inc.remediation_plan = {"justification": "revert to stable"}

    await mem.store_incident(inc)
    record = await mem.get_incident(inc.id)

    assert record is not None
    symptoms = record.symptoms
    for observed in (
        "payment-service",
        "db_connection_pool",
        "95.0",
        "80.0",
        "critical",
        "Checkout errors above SLO",
        "p99 latency 4s on /checkout",
    ):
        assert observed in symptoms, observed
    for model_output in ("orders-db", "connection leak", "deploy v2.3", "revert to stable"):
        assert model_output not in symptoms, model_output
    assert "bad_deploy_01" not in symptoms
    # The stored vector is the embedding of exactly that observed text.
    assert record.embedding == pytest.approx(await client.embed(symptoms), abs=1e-6)


async def test_store_rejects_a_non_uuid_id(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    inc.id = "inc-1a2b3c4d"
    with pytest.raises(SentinelMemoryError, match="not a UUID"):
        await mem.store_incident(inc)


async def test_store_protocol_delegates_to_store_incident(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store(inc)
    assert await mem.get_incident(inc.id) is not None


async def test_store_protocol_raises_type_error_for_non_incident(mem: EpisodicMemory) -> None:
    with pytest.raises(TypeError, match="Incident"):
        await mem.store({"id": "not-an-incident"})


# ── search_similar / query — pgvector <=> ranking ─────────────────────────────


async def test_search_similar_returns_nearest_first(mem: EpisodicMemory) -> None:
    """Store 3 incidents in different clusters; the matching one ranks first."""
    db_inc = _make_incident("payment-service", "db_connection_pool")
    mem_inc = _make_incident("analytics-pipeline", "memory_usage_percent", AlertSeverity.HIGH)
    other_inc = _make_incident("api-gateway", "error_rate")
    for inc in (db_inc, mem_inc, other_inc):
        await mem.store_incident(inc)

    results = await mem.search_similar("database connection pool exhausted", top_k=3)
    assert [r.id for r in results][0] == db_inc.id
    assert {r.id for r in results} == {db_inc.id, mem_inc.id, other_inc.id}
    assert all(isinstance(r, EpisodicRecord) for r in results)

    results = await mem.search_similar("memory leak oom heap usage high", top_k=3)
    assert results[0].id == mem_inc.id


async def test_query_scores_parallel_and_descending(mem: EpisodicMemory) -> None:
    db_inc = _make_incident("payment-service", "db_connection_pool")
    mem_inc = _make_incident("analytics-pipeline", "memory_usage_percent")
    await mem.store_incident(db_inc)
    await mem.store_incident(mem_inc)

    result = await mem.query("database connection pool exhausted", top_k=2)
    assert isinstance(result, MemoryQueryResult)
    assert [r.id for r in result.records] == [db_inc.id, mem_inc.id]
    assert result.similarity_scores == pytest.approx([1.0, 0.0], abs=1e-6)


@pytest.mark.parametrize(
    "as_vector",
    [list, tuple, lambda v: np.array(v, dtype=np.float32)],
    ids=["list", "tuple", "numpy"],
)
async def test_query_accepts_an_embedding_vector(
    mem: EpisodicMemory, as_vector: Callable[[list[float]], Any]
) -> None:
    mem_inc = _make_incident("analytics-pipeline", "memory_usage_percent")
    await mem.store_incident(_make_incident("payment-service", "db_connection_pool"))
    await mem.store_incident(mem_inc)
    result = await mem.query(as_vector(_one_hot(1)), top_k=1)
    assert [r.id for r in result.records] == [mem_inc.id]


async def test_search_top_k_limits_results(mem: EpisodicMemory) -> None:
    for i in range(5):
        await mem.store_incident(_make_incident("payment-service", f"db_connection_pool_{i}"))
    assert len(await mem.search_similar("database connection pool", top_k=2)) == 2


async def test_search_returns_every_row_when_top_k_exceeds_count(mem: EpisodicMemory) -> None:
    """ivfflat.probes covers every list, so a small table never loses rows to the index."""
    metrics = ["db_connection_pool", "memory_usage", "null_pointer_deploy", "error_rate"]
    stored = []
    for i in range(12):
        inc = _make_incident(f"svc-{i}", metrics[i % 4])
        await mem.store_incident(inc)
        stored.append(inc.id)
    result = await mem.query("database connection pool", top_k=50)
    assert sorted(r.id for r in result.records) == sorted(stored)


async def test_search_empty_table_returns_empty(mem: EpisodicMemory) -> None:
    assert await mem.search_similar("any symptoms here") == []
    result = await mem.query("database connection pool")
    assert result.records == []
    assert result.similarity_scores == []


# ── search_by_embedding ───────────────────────────────────────────────────────


async def test_search_by_embedding_ranks_by_cosine_distance(mem: EpisodicMemory) -> None:
    db_inc = _make_incident("payment-service", "db_connection_pool")
    mem_inc = _make_incident("analytics-pipeline", "memory_usage_percent")
    deploy_inc = _make_incident("api-gateway", "null_pointer_deploy")
    for inc in (db_inc, mem_inc, deploy_inc):
        await mem.store_incident(inc)

    # Mostly dim 2, a little dim 0: deploy first, then db, then memory (orthogonal).
    query = [0.0] * DIMS
    query[2], query[0] = 0.9, 0.3
    result = await mem.search_by_embedding(query, top_k=3)

    assert [r.id for r in result.records] == [deploy_inc.id, db_inc.id, mem_inc.id]
    norm = (0.9**2 + 0.3**2) ** 0.5
    assert result.similarity_scores == pytest.approx([0.9 / norm, 0.3 / norm, 0.0], abs=1e-6)


async def test_search_by_embedding_top_k(mem: EpisodicMemory) -> None:
    for metric in ("db_connection_pool", "memory_usage", "error_rate"):
        await mem.store_incident(_make_incident("svc", metric))
    result = await mem.search_by_embedding(_one_hot(3), top_k=1)
    assert len(result.records) == len(result.similarity_scores) == 1
    assert result.similarity_scores[0] == pytest.approx(1.0, abs=1e-6)


async def test_search_by_embedding_rejects_wrong_dimensions(mem: EpisodicMemory) -> None:
    await mem.store_incident(_make_incident("payment-service", "db_connection_pool"))
    with pytest.raises(SentinelMemoryError):
        await mem.search_by_embedding([1.0, 0.0, 0.0, 0.0])


async def test_search_by_embedding_rejects_non_positive_top_k(mem: EpisodicMemory) -> None:
    with pytest.raises(ValueError, match="top_k"):
        await mem.search_by_embedding(_one_hot(0), top_k=0)


# ── list_incidents ────────────────────────────────────────────────────────────


async def test_list_incidents_filters_by_status_and_service(mem: EpisodicMemory) -> None:
    pay_resolved = _make_incident("payment-service", "db_pool", status=IncidentStatus.RESOLVED)
    pay_escalated = _make_incident("payment-service", "oom", status=IncidentStatus.ESCALATED)
    api_resolved = _make_incident("api-gateway", "error_rate", status=IncidentStatus.RESOLVED)
    for inc in (pay_resolved, pay_escalated, api_resolved):
        await mem.store_incident(inc)

    def ids(records: list[EpisodicRecord]) -> set[str]:
        return {r.id for r in records}

    assert ids(await mem.list_incidents()) == {pay_resolved.id, pay_escalated.id, api_resolved.id}
    assert ids(await mem.list_incidents(status=IncidentStatus.RESOLVED)) == {
        pay_resolved.id,
        api_resolved.id,
    }
    assert ids(await mem.list_incidents(status="escalated")) == {pay_escalated.id}
    assert ids(await mem.list_incidents(service="payment-service")) == {
        pay_resolved.id,
        pay_escalated.id,
    }
    assert ids(await mem.list_incidents(status="resolved", service="api-gateway")) == {
        api_resolved.id
    }
    assert await mem.list_incidents(status="rollback_proposed") == []
    assert await mem.list_incidents(service="no-such-service") == []


async def test_list_incidents_newest_first_and_limited(mem: EpisodicMemory) -> None:
    base = datetime(2026, 10, 1, tzinfo=UTC)
    stored = []
    for hours in (1, 3, 2):
        inc = _make_incident("payment-service", "db_pool")
        inc.created_at = base + timedelta(hours=hours)
        await mem.store_incident(inc)
        stored.append((inc.created_at, inc.id))
    newest_first = [inc_id for _, inc_id in sorted(stored, reverse=True)]

    assert [r.id for r in await mem.list_incidents()] == newest_first
    assert [r.id for r in await mem.list_incidents(limit=2)] == newest_first[:2]
    with pytest.raises(ValueError, match="limit"):
        await mem.list_incidents(limit=0)


# ── get / delete ──────────────────────────────────────────────────────────────


async def test_get_protocol_returns_episodic_record(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    result = await mem.get(inc.id)
    assert isinstance(result, EpisodicRecord)
    assert result.id == inc.id


async def test_get_missing_returns_none(mem: EpisodicMemory) -> None:
    assert await mem.get_incident(str(uuid.uuid4())) is None
    assert await mem.get("inc-missing") is None  # not a UUID: cannot exist


async def test_delete_removes_only_that_record(mem: EpisodicMemory) -> None:
    inc_a = _make_incident("payment-service", "db_connection_pool")
    inc_b = _make_incident("analytics-pipeline", "memory_usage_percent")
    await mem.store_incident(inc_a)
    await mem.store_incident(inc_b)

    await mem.delete(inc_a.id)

    assert await mem.get_incident(inc_a.id) is None
    assert await mem.get_incident(inc_b.id) is not None
    results = await mem.search_similar("database connection pool exhausted", top_k=5)
    assert [r.id for r in results] == [inc_b.id]


async def test_delete_nonexistent_is_noop(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    await mem.delete(str(uuid.uuid4()))
    await mem.delete("inc-nonexistent")
    assert await mem.get_incident(inc.id) is not None


# ── search_past_incidents tool over real memory ───────────────────────────────


async def _set_resolution_type(pool: asyncpg.Pool, incident_id: str, value: str) -> None:
    """Write resolution_type as the phase-4 pipeline would (store_incident leaves it NULL)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE incidents SET resolution_type = $2 WHERE id = $1", uuid.UUID(incident_id), value
        )


async def test_resolution_type_reads_back(mem: EpisodicMemory, pool: asyncpg.Pool) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    record = await mem.get_incident(inc.id)
    assert record is not None
    assert record.resolution_type is None

    await _set_resolution_type(pool, inc.id, "rollback")
    record = await mem.get_incident(inc.id)
    assert record is not None
    assert record.resolution_type == "rollback"


async def test_incident_search_tool_ranks_nearest_first(
    mem: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    deploy = _make_incident("api-gateway", "null_pointer_deploy")
    pool_inc = _make_incident("payment-service", "db_connection_pool")
    oom = _make_incident("analytics-pipeline", "memory_leak_oom")
    for inc in (deploy, pool_inc, oom):
        await mem.store_incident(inc)
    await _set_resolution_type(pool, pool_inc.id, "rollback")

    result = await _search_past_incidents(
        mem, IncidentSearchInput(symptom_text="database connection pool", k=3)
    )

    assert isinstance(result, list)
    assert len(result) == 3
    top = result[0]
    assert isinstance(top, SimilarIncident)
    assert (top.id, top.service, top.resolution_type) == (
        pool_inc.id,
        "payment-service",
        "rollback",
    )
    assert top.created_at == pool_inc.created_at
    assert top.similarity == pytest.approx(1.0, abs=1e-6)
    assert top.model_generated is True
    assert [r.similarity for r in result] == sorted((r.similarity for r in result), reverse=True)


async def test_incident_search_tool_respects_k(mem: EpisodicMemory) -> None:
    for metric in ("db_connection_pool", "null_pointer_deploy", "memory_leak_oom"):
        await mem.store_incident(_make_incident("payment-service", metric))
    result = await _search_past_incidents(
        mem, IncidentSearchInput(symptom_text="database connection pool", k=2)
    )
    assert isinstance(result, list)
    assert len(result) == 2


async def test_incident_search_tool_empty_table_returns_empty_list(mem: EpisodicMemory) -> None:
    assert await _search_past_incidents(mem, IncidentSearchInput(symptom_text="pool")) == []


async def test_incident_search_tool_reports_memory_unavailable(client: EmbeddingClient) -> None:
    result = await _search_past_incidents(
        EpisodicMemory(None, client), IncidentSearchInput(symptom_text="pool")
    )
    assert isinstance(result, ToolFailure)
    assert result.kind == "unavailable"
