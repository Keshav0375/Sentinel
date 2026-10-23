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
from sentinel.models.alert import IncidentAlert, IncidentTags
from sentinel.models.incident import Incident, IncidentStatus, Severity, TimelineEntry
from sentinel.models.memory import (
    EpisodicRecord,
    IncidentOutcome,
    IncidentRow,
    MemoryQueryResult,
)
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
    severity: str = "critical",
    status: IncidentStatus = IncidentStatus.RESOLVED,
) -> Incident:
    alert = IncidentAlert(title=metric, severity=severity, service=service)
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
        "get_incident_row": lambda m: m.get_incident_row(some_id),
        "list_incident_rows": lambda m: m.list_incident_rows(),
        "stored_incident_ids": lambda m: m.stored_incident_ids([]),  # empty: still raises
        "get_linked_deployment": lambda m: m.get_linked_deployment(some_id),
        "find_incident_by_correlation_id": lambda m: m.find_incident_by_correlation_id(some_id),
        "link_deployment": lambda m: m.link_deployment(some_id, some_id),
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
    inc.timeline = [
        TimelineEntry(agent_name="triage", action="classify", result_summary="P1"),
        TimelineEntry(agent_name="analysis", action="fetch_logs", result_summary="leak"),
    ]
    outcome = _outcome(
        root_cause="connection leak in checkout handler",
        root_cause_by="analysis",
        resolution="revert to stable",
    )

    await mem.store_incident(inc, outcome=outcome)
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
        "analysis",
    ]
    provenance = record.raw_timeline["provenance"]
    assert provenance["root_cause_by"] == "analysis"
    assert provenance["resolution_by"] == "resolution"
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
    inc = _make_incident("payment-service", "db_connection_pool", "high")
    inc.severity = None
    inc.affected_service = None
    await mem.store_incident(inc)
    record = await mem.get_incident(inc.id)
    assert record is not None
    assert record.severity == "high"
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
    await mem.store_incident(inc, outcome=_outcome(resolution="rollback"))

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


async def test_without_an_outcome_the_timeline_attributes_nothing(mem: EpisodicMemory) -> None:
    """Only the pipeline's outcome names a conclusion and its agent; a timeline does not."""
    inc = _make_incident("payment-service", "db_connection_pool")
    inc.timeline = [
        TimelineEntry(agent_name="analysis", action="fetch_logs", result_summary="leak"),
        TimelineEntry(agent_name="resolution", action="draft", result_summary="ok"),
    ]

    await mem.store_incident(inc)
    record = await mem.get_incident(inc.id)

    assert record is not None and record.raw_timeline is not None
    assert record.root_cause == "unknown" and record.resolution == "unresolved"
    provenance = record.raw_timeline["provenance"]
    assert provenance["root_cause_by"] == "unknown"
    assert provenance["resolution_by"] == "unknown"
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
    inc.alert = inc.alert.model_copy(
        update={
            "title": "Checkout errors above SLO",
            "tags": IncidentTags(deploy_status="failed", failed_stage="verify", version="v9"),
        }
    )
    inc.affected_service = "orders-db"  # triage's conclusion
    outcome = _outcome(
        root_cause="connection leak in checkout handler after deploy v2.3",
        resolution="revert to stable",
    )

    await mem.store_incident(inc, outcome=outcome)
    record = await mem.get_incident(inc.id)

    assert record is not None
    symptoms = record.symptoms
    for observed in (
        "payment-service",
        "critical",
        "Checkout errors above SLO",
        "deploy_status failed",
        "failed_stage verify",
    ):
        assert observed in symptoms, observed
    for model_output in ("orders-db", "connection leak", "deploy v2.3", "revert to stable"):
        assert model_output not in symptoms, model_output
    assert "v9" not in symptoms  # the version tag is not part of the symptoms
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
    mem_inc = _make_incident("analytics-pipeline", "memory_usage_percent", "high")
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


# ── store_incident(outcome=…) — the phase-4 pipeline's columns ────────────────


def _outcome(**overrides: Any) -> IncidentOutcome:
    fields: dict[str, Any] = {
        "correlation_id": str(uuid.uuid4()),
        "root_cause": "pr-42 changed GET / to return HTML",
        "root_cause_by": "resolution",
        "root_cause_confidence": 0.85,
        "resolution": "revert pr-42",
        "resolution_by": "resolution",
        "resolution_type": "rollback",
        "reflexion_loops": 1,
        "langfuse_trace_id": uuid.uuid4().hex,
        "pipeline_result": {"status": "rollback_proposed"},
    }
    return IncidentOutcome(**(fields | overrides))


async def test_an_outcome_writes_its_columns_and_provenance(
    mem: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    inc = _make_incident("api-gateway", "error_rate", status=IncidentStatus.ROLLBACK_PROPOSED)
    outcome = _outcome()
    await mem.store_incident(inc, outcome=outcome)

    row = await _raw_row(pool, inc.id)
    assert row["status"] == "rollback_proposed"
    assert row["correlation_id"] == uuid.UUID(outcome.correlation_id)
    assert row["root_cause"] == outcome.root_cause and row["resolution"] == outcome.resolution
    assert row["root_cause_confidence"] == pytest.approx(0.85)
    assert row["resolution_type"] == "rollback"
    assert row["reflexion_loops"] == 1
    assert row["langfuse_trace_id"] == outcome.langfuse_trace_id
    trajectory = json.loads(row["trajectory"])
    assert trajectory["pipeline_result"] == {"status": "rollback_proposed"}
    assert trajectory["provenance"]["root_cause_by"] == "resolution"
    record = await mem.get_incident(inc.id)
    assert record is not None and record.resolution_type == "rollback"


async def test_a_store_without_an_outcome_leaves_the_outcome_columns(
    mem: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    inc = _make_incident("api-gateway", "error_rate", status=IncidentStatus.ESCALATED)
    await mem.store_incident(
        inc, outcome=_outcome(resolution_type="escalated", correlation_id=None)
    )
    await mem.store_incident(inc)

    row = await _raw_row(pool, inc.id)
    assert row["resolution_type"] == "escalated" and row["reflexion_loops"] == 1
    assert row["correlation_id"] is None
    assert "pipeline_result" not in json.loads(row["trajectory"])


# ── Read API for GET /incidents* ──────────────────────────────────────────────


async def test_get_incident_row_returns_the_full_row(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool", status=IncidentStatus.ESCALATED)
    outcome = IncidentOutcome(
        correlation_id=str(uuid.uuid4()),
        root_cause="rc",
        root_cause_by="analysis",
        root_cause_confidence=0.8,
        resolution="r",
        resolution_by="resolution",
        resolution_type="rollback",
        reflexion_loops=1,
        langfuse_trace_id="a" * 32,
        pipeline_result={"status": "rollback_proposed"},
    )
    await mem.store_incident(inc, outcome=outcome)

    row = await mem.get_incident_row(inc.id)

    assert row is not None
    assert row.id == inc.id and row.service == "payment-service"
    assert row.status == inc.status.value
    assert row.correlation_id == outcome.correlation_id
    assert row.root_cause == "rc" and row.resolution_type == "rollback"
    assert row.root_cause_confidence == pytest.approx(0.8)
    assert row.reflexion_loops == 1 and row.langfuse_trace_id == "a" * 32
    assert isinstance(row.trajectory, dict)
    assert row.trajectory["pipeline_result"] == {"status": "rollback_proposed"}
    assert row.eval_score is None and row.pr_number is None
    assert "embedding" not in IncidentRow.model_fields
    assert await mem.get_incident_row("not-a-uuid") is None
    assert await mem.get_incident_row(str(uuid.uuid4())) is None


async def test_list_incident_rows_filters_and_orders_like_list_incidents(
    mem: EpisodicMemory,
) -> None:
    older = _make_incident("payment-service", "cpu", status=IncidentStatus.RESOLVED)
    older.created_at = datetime(2026, 10, 1, tzinfo=UTC)
    newer = _make_incident("api-gateway", "error_rate", status=IncidentStatus.ESCALATED)
    newer.created_at = datetime(2026, 10, 2, tzinfo=UTC)
    await mem.store_incident(older)
    await mem.store_incident(newer)

    assert [r.id for r in await mem.list_incident_rows()] == [newer.id, older.id]
    only = await mem.list_incident_rows(status="escalated")
    assert [r.id for r in only] == [newer.id]
    assert [r.id for r in await mem.list_incident_rows(service="payment-service")] == [older.id]
    assert len(await mem.list_incident_rows(limit=1)) == 1
    with pytest.raises(ValueError, match="limit"):
        await mem.list_incident_rows(limit=0)


async def test_stored_incident_ids_returns_the_given_ids_with_a_row(mem: EpisodicMemory) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)

    given = inc.id.upper()
    found = await mem.stored_incident_ids([given, str(uuid.uuid4()), "not-a-uuid"])

    assert found == {given}
    assert await mem.stored_incident_ids([]) == set()


async def test_get_linked_deployment_returns_the_newest_linked_row(
    mem: EpisodicMemory, pool: asyncpg.Pool
) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    bare = _make_incident("api-gateway", "error_rate")
    await mem.store_incident(inc)
    await mem.store_incident(bare)
    t0 = datetime(2026, 10, 1, tzinfo=UTC)

    async def deploy(sha: str, incident_id: str | None, deployed_at: datetime) -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO deployments (service, commit_sha, deploy_status, incident_id,"
                " deployed_at, files_changed, metadata) VALUES"
                " ('payment-service', $1, 'failed', $2, $3, '[\"a.py\"]'::jsonb,"
                " '{\"k\": 1}'::jsonb)",
                sha,
                None if incident_id is None else uuid.UUID(incident_id),
                deployed_at,
            )

    await deploy("old0000", inc.id, t0)
    await deploy("new1111", inc.id, t0 + timedelta(hours=1))
    await deploy("free222", None, t0 + timedelta(hours=2))

    linked = await mem.get_linked_deployment(inc.id)

    assert linked is not None
    assert linked.commit_sha == "new1111" and linked.incident_id == inc.id
    assert linked.files_changed == ["a.py"] and linked.metadata == {"k": 1}
    assert await mem.get_linked_deployment(bare.id) is None
    assert await mem.get_linked_deployment("not-a-uuid") is None


async def test_find_incident_by_correlation_id(mem: EpisodicMemory, pool: asyncpg.Pool) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    correlation_id = str(uuid.uuid4())
    await mem.store_incident(inc, outcome=_outcome(correlation_id=correlation_id))

    assert await mem.find_incident_by_correlation_id(correlation_id) == inc.id
    assert await mem.find_incident_by_correlation_id(correlation_id.upper()) == inc.id
    assert await mem.find_incident_by_correlation_id("not-a-uuid") is None
    assert await mem.find_incident_by_correlation_id(str(uuid.uuid4())) is None


async def test_link_deployment(mem: EpisodicMemory, pool: asyncpg.Pool) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    await mem.store_incident(inc)
    async with pool.acquire() as conn:
        deploy_id = await conn.fetchval(
            "INSERT INTO deployments (service, commit_sha, deploy_status, deployed_at)"
            " VALUES ('payment-service', 'abc1234', 'failed', now()) RETURNING id"
        )

    assert await mem.link_deployment(inc.id, str(deploy_id)) is True
    linked = await mem.get_linked_deployment(inc.id)
    assert linked is not None and linked.id == str(deploy_id)
    assert await mem.link_deployment(inc.id, str(uuid.uuid4())) is False
    with pytest.raises(SentinelMemoryError, match="not a UUID"):
        await mem.link_deployment(inc.id, "nope")
    with pytest.raises(SentinelMemoryError, match="not a UUID"):
        await mem.link_deployment("nope", str(deploy_id))


async def test_dd_event_id_is_stored(mem: EpisodicMemory, pool: asyncpg.Pool) -> None:
    inc = _make_incident("payment-service", "db_connection_pool")
    inc.alert = inc.alert.model_copy(update={"dd_event_id": "evt-123"})
    await mem.store_incident(inc)

    assert (await _raw_row(pool, inc.id))["dd_event_id"] == "evt-123"
    inc.alert = inc.alert.model_copy(update={"dd_event_id": "evt-456"})
    await mem.store_incident(inc)  # the upsert updates it too
    assert (await _raw_row(pool, inc.id))["dd_event_id"] == "evt-456"
