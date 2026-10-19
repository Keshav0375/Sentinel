"""Incident models — Severity, IncidentStatus, TimelineEntry, TriageResult, Incident."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from sentinel.models.alert import AlertPayload


class Severity(StrEnum):
    """Internal incident severity, assigned by the Triage Agent.

    Distinct from AlertSeverity (the raw value from the monitoring tool).
    P1 = production down / revenue impacting.
    P2 = major degradation, most users affected.
    P3 = minor degradation, subset of users affected.
    P4 = cosmetic / no direct user impact.
    """

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class IncidentStatus(StrEnum):
    """Lifecycle state of an incident as it moves through the pipeline.

    The pipeline ends in ``ROLLBACK_PROPOSED`` (a rollback spec for GitHub Actions to turn
    into a revert PR a human merges or closes) or ``ESCALATED`` (decision 2026-10-15).
    ``RESOLVED`` is reserved for a later "revert deploy succeeded" signal; the backend never
    sets it, and there is no approval state — the revert PR on GitHub is the human gate.
    """

    TRIAGE = "triage"
    INVESTIGATING = "investigating"
    REMEDIATING = "remediating"
    ROLLBACK_PROPOSED = "rollback_proposed"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class TimelineEntry(BaseModel):
    """A single step recorded in the incident timeline.

    Every agent tool call and handoff appends one of these to Incident.timeline
    so the eval system can reconstruct the full trajectory.
    """

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    agent_name: str
    action: str
    result_summary: str


class TriageResult(BaseModel):
    """Structured output produced by the Triage Agent.

    The agent populates this after calling get_service_metadata and
    search_past_incidents. It drives every downstream decision: severity
    determines SLA, affected_service scopes log and deploy queries,
    is_duplicate short-circuits re-investigation.
    """

    severity: Severity
    affected_service: str
    is_duplicate: bool
    recommended_action: str


class Incident(BaseModel):
    """Full state of a live or resolved incident.

    ``triage_result`` is the triage agent's output as a JSON-serialisable dict. The
    pipeline's conclusions (root cause, rollback spec or escalation) are not fields here:
    they travel in ``PipelineResult`` and the ``IncidentOutcome`` stored with the incident.
    """

    # A UUID string: episodic memory stores it as incidents.id (UUID, §5.3).
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alert: AlertPayload
    status: IncidentStatus = IncidentStatus.TRIAGE
    severity: Severity | None = None
    affected_service: str | None = None
    timeline: list[TimelineEntry] = []  # pydantic v2 deep-copies mutable defaults
    triage_result: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
