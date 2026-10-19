"""Memory models — EpisodicRecord, IncidentOutcome, Runbook, SemanticRecord, MemoryQueryResult."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from sentinel.models.service import ServiceMetadata


class EpisodicRecord(BaseModel):
    """A past resolved incident stored in episodic memory.

    The embedding is the vector representation of the symptoms field, used for
    cosine-similarity search when a new incident arrives. Stored as a BLOB in
    SQLite and deserialized with numpy.
    """

    id: str
    service_name: str
    severity: str
    symptoms: str
    root_cause: str
    resolution: str
    # Read-only here: incidents.resolution_type is written by the phase-4 pipeline;
    # store_incident leaves it NULL (decision 2026-10-12, item 3).
    resolution_type: str | None = None
    mttr_seconds: int | None = None
    embedding: list[float] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_timeline: dict[str, Any] | None = None


class Runbook(BaseModel):
    """Ordered remediation steps for a specific failure class on a service.

    Parsed from the markdown in services.runbook (one per service). Referenced by
    the Triage and Remediation agents to guide their decision-making.
    """

    id: str
    service_name: str
    failure_class: str
    steps: list[str]
    # Set by get_service_metadata when it cut steps to keep its output small.
    truncated: bool = False


class SemanticRecord(BaseModel):
    """Combined view of a service's metadata and associated runbooks.

    Loaded from semantic memory (PostgreSQL) once per incident. The Triage Agent
    uses this to understand service topology and available remediation playbooks.
    """

    service: ServiceMetadata
    runbooks: list[Runbook] = []


class MemoryQueryResult(BaseModel):
    """Results from a similarity search against episodic memory.

    records and similarity_scores are parallel lists — scores[i] is the cosine
    similarity for records[i]. Results are returned in descending score order.
    """

    records: list[EpisodicRecord] = []
    similarity_scores: list[float] = []


class IncidentOutcome(BaseModel):
    """What the phase-4 pipeline adds to an ``incidents`` row (§5.3) beyond an Incident.

    Passed to ``EpisodicMemory.store_incident(incident, outcome=...)``: it supplies the
    columns a Phase-1 :class:`~sentinel.models.incident.Incident` cannot (correlation_id,
    root_cause_confidence, resolution_type, reflexion_loops, langfuse_trace_id), the
    root cause and resolution with the agent that produced each (``provenance``), and the
    pipeline result, kept in ``trajectory``. Agent output, not verified fact.
    """

    correlation_id: str | None = Field(default=None, description="The bridge's UUID (§2.1).")
    root_cause: str
    root_cause_by: str = Field(description="The agent (or the orchestrator) that said it.")
    root_cause_confidence: float = Field(ge=0.0, le=1.0)
    resolution: str
    resolution_by: str
    resolution_type: str = Field(description="rollback | escalated (§5.3).")
    reflexion_loops: int = Field(ge=0)
    langfuse_trace_id: str = Field(description="The incident UUID as 32 hex (§4.5).")
    pipeline_result: dict[str, Any] = Field(description="PipelineResult, as JSON.")
