"""Memory models — EpisodicRecord, Runbook, SemanticRecord, MemoryQueryResult."""

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
    mttr_seconds: int | None = None
    embedding: list[float] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_timeline: dict[str, Any] | None = None


class Runbook(BaseModel):
    """Ordered remediation steps for a specific failure class on a service.

    Stored in the SQLite runbooks table. Referenced by the Triage and
    Remediation agents to guide their decision-making.
    """

    id: str
    service_name: str
    failure_class: str
    steps: list[str]


class SemanticRecord(BaseModel):
    """Combined view of a service's metadata and associated runbooks.

    Loaded from semantic memory (SQLite) once per incident. The Triage Agent
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
