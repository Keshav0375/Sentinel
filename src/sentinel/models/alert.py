"""Alert models — AlertSource, AlertSeverity, AlertPayload, AlertAck.

These are the contract between the webhook receiver and the orchestrator.
AlertPayload is what arrives from Datadog/PagerDuty; AlertAck is what we
send back to confirm receipt and assign an internal incident ID.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AlertSource(StrEnum):
    """Monitoring system that fired the alert."""

    DATADOG = "datadog"
    PAGERDUTY = "pagerduty"


class AlertSeverity(StrEnum):
    """Raw severity as reported by the monitoring tool.

    Distinct from incident.Severity (P1-P4), which is Sentinel's internal
    classification assigned by the Triage Agent after context enrichment.
    """

    CRITICAL = "critical"
    HIGH = "high"
    WARNING = "warning"
    LOW = "low"
    INFO = "info"


class AlertPayload(BaseModel):
    """Incoming alert from a monitoring integration.

    alert_id and timestamp have defaults so synthetic scenario payloads don't
    need to supply them explicitly.
    """

    alert_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: AlertSource
    service: str
    metric: str
    threshold: float
    current_value: float
    severity: AlertSeverity
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class AlertAck(BaseModel):
    """Acknowledgement returned to the caller after an alert is accepted.

    incident_id is assigned by the webhook receiver and ties every downstream
    agent action back to this alert.
    """

    alert_id: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    incident_id: str
