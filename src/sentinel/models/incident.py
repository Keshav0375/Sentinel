"""Incident models — Severity, IncidentStatus, TimelineEntry, Incident."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

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
    """Lifecycle state of an incident as it moves through the pipeline."""

    TRIAGE = "triage"
    INVESTIGATING = "investigating"
    REMEDIATING = "remediating"
    PENDING_APPROVAL = "pending_approval"
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


class IncidentSummary(BaseModel):
    """Final outcome returned by the Orchestrator Agent after the full pipeline runs.

    This is what the API layer surfaces to callers and what the eval system
    uses to determine whether the pipeline reached a terminal state correctly.
    """

    status: Literal["resolved", "pending_approval", "escalated"]
    affected_service: str
    severity: Severity
    root_cause_summary: str
    action_taken: str
    next_steps: list[str] = []


class Incident(BaseModel):
    """Full state of a live or resolved incident.

    Result fields (triage_result, log_analysis, deploy_correlation,
    remediation_plan) are typed as dict[str, Any] | None because their
    structured types (TriageResult, LogAnalysis, etc.) are defined in later
    phases. They will be populated by agents as JSON-serialisable dicts and
    re-typed once those models exist.
    """

    id: str = Field(default_factory=lambda: f"inc-{uuid.uuid4().hex[:8]}")
    alert: AlertPayload
    status: IncidentStatus = IncidentStatus.TRIAGE
    severity: Severity | None = None
    affected_service: str | None = None
    timeline: list[TimelineEntry] = []  # pydantic v2 deep-copies mutable defaults
    triage_result: dict[str, Any] | None = None
    log_analysis: dict[str, Any] | None = None
    deploy_correlation: dict[str, Any] | None = None
    remediation_plan: dict[str, Any] | None = None
    comms_summary: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
