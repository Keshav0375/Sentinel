"""Scenario definitions — Pydantic model and JSON loader."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from sentinel.models.alert import AlertSeverity, AlertSource
from sentinel.models.incident import Severity
from sentinel.models.log_entry import LogLevel
from sentinel.models.remediation import RemediationAction


class ScenarioAlert(BaseModel):
    """Alert fields as stored in a scenario JSON file.

    A subset of AlertPayload — the fields present in the static file.
    alert_id and timestamp are generated at runtime by generate_alert().
    """

    source: AlertSource
    service: str
    metric: str
    threshold: float
    current_value: float
    severity: AlertSeverity


class KnownRootCause(BaseModel):
    """Technical root cause of the incident.

    deploy_id and commit are None for scenarios where the failure is not
    caused by a code deploy (e.g. downstream_outage, config_regression).
    """

    type: str
    deploy_id: str | None
    commit: str | None
    description: str


class GroundTruth(BaseModel):
    """Expected agent outputs — used by the eval judge for scoring.

    Structured answer key that the LLM-as-judge compares against the
    agent's actual trajectory. Separate from KnownRootCause because the
    judge needs pre-parsed fields (severity, recommended_action) rather
    than free-text descriptions.
    """

    severity: Severity
    affected_service: str
    root_cause_summary: str
    recommended_action: RemediationAction
    deploy_id: str | None


class ScenarioLogEntry(BaseModel):
    """A single log line as stored in the scenario JSON.

    Uses 'ts' (not 'timestamp') to match the compact JSON field name.
    The log_gen module converts these to LogEntry objects with 'timestamp'.
    """

    ts: datetime
    level: LogLevel
    service: str
    message: str
    trace_id: str | None = None


class ScenarioDeploy(BaseModel):
    """A deploy record as stored in the scenario JSON.

    Uses 'ts' and 'commit' to match the compact JSON field names.
    The deploy_gen module converts these to Deploy objects.
    commit is nullable — config-only changes have no associated commit.
    """

    id: str
    service: str
    ts: datetime
    author: str
    commit: str | None
    files_changed: int
    description: str = ""


class Scenario(BaseModel):
    """Complete incident scenario loaded from a JSON file.

    Contains everything needed to drive one end-to-end agent run:
    alert payload, synthetic logs and deploys, and ground truth for
    eval scoring. Loaded via alert_gen.load_scenario().
    """

    scenario_id: str
    failure_class: str
    description: str
    alert: ScenarioAlert
    known_root_cause: KnownRootCause
    ground_truth: GroundTruth
    logs: list[ScenarioLogEntry] = []
    deploys: list[ScenarioDeploy] = []
