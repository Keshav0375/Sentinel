"""Tests for sentinel.models.memory."""

from __future__ import annotations

from sentinel.models.memory import (
    EpisodicRecord,
    MemoryQueryResult,
    Runbook,
    SemanticRecord,
)
from sentinel.models.service import ServiceMetadata, ServiceTier

# ── EpisodicRecord ────────────────────────────────────────────────────────────


def test_episodic_record_required_fields() -> None:
    rec = EpisodicRecord(
        id="ep-001",
        service_name="api-gateway",
        severity="P1",
        symptoms="error_rate spike to 34%, NullPointerException in auth middleware",
        root_cause="bad_deploy: missing config key in deploy-abc123",
        resolution="rollback deploy-abc123",
    )
    assert rec.id == "ep-001"
    assert rec.service_name == "api-gateway"
    assert rec.mttr_seconds is None
    assert rec.embedding == []
    assert rec.raw_timeline is None
    assert rec.created_at.tzinfo is not None


def test_episodic_record_with_embedding() -> None:
    embedding = [0.1] * 384
    rec = EpisodicRecord(
        id="ep-002",
        service_name="user-service",
        severity="P2",
        symptoms="connection pool exhaustion",
        root_cause="db_pool: unclosed cursors in batch job",
        resolution="hotfix: added cursor.close() in finally block",
        mttr_seconds=240,
        embedding=embedding,
    )
    assert len(rec.embedding) == 384
    assert rec.mttr_seconds == 240


def test_episodic_record_with_timeline() -> None:
    rec = EpisodicRecord(
        id="ep-003",
        service_name="svc",
        severity="P3",
        symptoms="slow responses",
        root_cause="downstream timeout",
        resolution="escalated to partner team",
        raw_timeline={"timeline": [{"agent": "triage", "action": "classify"}]},
    )
    assert rec.raw_timeline is not None
    assert "timeline" in rec.raw_timeline


def test_episodic_record_json_round_trip() -> None:
    rec = EpisodicRecord(
        id="ep-rt",
        service_name="svc",
        severity="P1",
        symptoms="OOM",
        root_cause="memory leak",
        resolution="restart + fix",
        embedding=[0.5, -0.3, 0.1],
    )
    reloaded = EpisodicRecord.model_validate_json(rec.model_dump_json())
    assert reloaded.id == rec.id
    assert reloaded.embedding == rec.embedding


# ── Runbook ───────────────────────────────────────────────────────────────────


def test_runbook_fields() -> None:
    rb = Runbook(
        id="rb-001",
        service_name="api-gateway",
        failure_class="bad_deploy",
        steps=["check deploy history", "identify suspect commit", "rollback"],
    )
    assert rb.failure_class == "bad_deploy"
    assert len(rb.steps) == 3


# ── SemanticRecord ────────────────────────────────────────────────────────────


def test_semantic_record_defaults() -> None:
    svc = ServiceMetadata(name="api-gateway", team="team-platform", tier=ServiceTier.CRITICAL)
    sr = SemanticRecord(service=svc)
    assert sr.service.name == "api-gateway"
    assert sr.runbooks == []


def test_semantic_record_with_runbooks() -> None:
    svc = ServiceMetadata(name="api-gateway", team="team-platform", tier=ServiceTier.CRITICAL)
    rb = Runbook(
        id="rb-001",
        service_name="api-gateway",
        failure_class="bad_deploy",
        steps=["check deploys", "rollback"],
    )
    sr = SemanticRecord(service=svc, runbooks=[rb])
    assert len(sr.runbooks) == 1
    assert sr.runbooks[0].failure_class == "bad_deploy"


# ── MemoryQueryResult ─────────────────────────────────────────────────────────


def test_memory_query_result_empty() -> None:
    result = MemoryQueryResult()
    assert result.records == []
    assert result.similarity_scores == []


def test_memory_query_result_with_data() -> None:
    rec1 = EpisodicRecord(
        id="ep-a", service_name="svc", severity="P1",
        symptoms="error spike", root_cause="bad deploy", resolution="rollback",
    )
    rec2 = EpisodicRecord(
        id="ep-b", service_name="svc", severity="P2",
        symptoms="slow queries", root_cause="pool exhaustion", resolution="restart",
    )
    result = MemoryQueryResult(
        records=[rec1, rec2],
        similarity_scores=[0.95, 0.72],
    )
    assert len(result.records) == 2
    assert result.similarity_scores[0] == 0.95
    assert result.records[0].id == "ep-a"
