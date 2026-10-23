"""Alert models — the ``POST /webhooks/incident`` body and what the pipeline sees of it.

``IncidentPayload`` is the §3.1 request body (with its tag / PR / context sub-models);
``IncidentAlert`` is the pipeline-facing alert built from it (no raw payload, signal type or
correlation id: those travel on ``PipelineInput``). The Phase-1 ``AlertPayload`` family stays
for the scenario generator and the eval runner until backend 9.1 deletes them;
``IncidentAlert.from_phase1`` adapts one.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

# §3.1: deploy_failure (case ii, rollback fast path) | runtime_error (case iii, full loop).
SignalType = Literal["deploy_failure", "runtime_error"]
SIGNAL_TYPES: Final[tuple[SignalType, ...]] = get_args(SignalType)
# Stand-in for a NOT NULL column whose source field is absent (never a guessed value).
UNKNOWN: Final = "unknown"
_TAG_KEYS: Final = ("service", "deploy_status", "failed_stage", "version")
# Bounds on the sender-controlled context that prompts embed (see ``bounded_context``).
MAX_RECENT_LOGS: Final = 50
MAX_METADATA_KEYS: Final = 50
MAX_CONTEXT_ITEM_CHARS: Final = 2000
TRUNCATED: Final = "…[truncated]"


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


class IncidentTags(BaseModel):
    """The tags the pipeline reads; any other key is ignored."""

    model_config = ConfigDict(extra="ignore")

    service: str | None = None
    deploy_status: str | None = None
    failed_stage: str | None = None
    version: str | None = None


class PRDetails(BaseModel):
    """The pull request behind the deploy (§3.1 ``context.pr_details``)."""

    model_config = ConfigDict(extra="ignore")

    number: int | None = None
    sha: str | None = None
    author: str | None = None
    files_changed: list[str] = []


class IncidentContext(BaseModel):
    """Optional enrichment the sender attaches (§3.1 ``context``)."""

    model_config = ConfigDict(extra="ignore")

    service_metadata: dict[str, JsonValue] | None = None
    pr_details: PRDetails | None = None
    recent_logs: list[JsonValue] = []


def parse_tags(value: object) -> object:
    """Normalise ``tags``: None -> ``{}``; a Datadog ``"k:v,k2:v2"`` string -> a dict.

    Splits on ``,``, strips, partitions on the FIRST ``:``, keeps only the keys the
    pipeline reads, skips items without a ``:`` or with an empty value; the last duplicate
    wins. Any other type is returned unchanged (pydantic then rejects it).
    """
    if value is None:
        return {}
    if isinstance(value, str):
        tags: dict[str, str] = {}
        for item in value.split(","):
            key, sep, val = item.strip().partition(":")
            key, val = key.strip(), val.strip()
            if sep and val and key in _TAG_KEYS:
                tags[key] = val
        return tags
    return value


class IncidentPayload(BaseModel):
    """The ``POST /webhooks/incident`` body (§3.1); unknown keys are ignored."""

    model_config = ConfigDict(extra="ignore")

    signal_type: SignalType
    correlation_id: str
    title: str
    source: str | None = None
    alert_id: str | None = None
    dd_event_id: str | None = None
    severity: str = UNKNOWN
    tags: IncidentTags = Field(default_factory=IncidentTags)
    context: IncidentContext = Field(default_factory=IncidentContext)
    timestamp: datetime | None = None
    raw_payload: JsonValue = None

    @field_validator("correlation_id")
    @classmethod
    def _correlation_is_uuid(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError(f"{value!r} is not a UUID") from exc

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def _parse_tags(cls, value: object) -> object:
        return parse_tags(value)

    @field_validator("context", mode="before")
    @classmethod
    def _context_default(cls, value: object) -> object:
        return {} if value is None else value


def _bounded_value(value: JsonValue) -> JsonValue:
    """*value*, or its JSON cut to ``MAX_CONTEXT_ITEM_CHARS`` plus ``TRUNCATED`` when longer."""
    encoded = json.dumps(value, default=str)
    if len(encoded) <= MAX_CONTEXT_ITEM_CHARS:
        return value
    return encoded[:MAX_CONTEXT_ITEM_CHARS] + TRUNCATED


def bounded_context(context: IncidentContext) -> IncidentContext:
    """*context* with ``recent_logs`` and ``service_metadata`` bounded; the rest untouched.

    Keeps the first ``MAX_RECENT_LOGS`` logs and ``MAX_METADATA_KEYS`` metadata keys (a
    marker records how many were dropped), and cuts any single item or value over
    ``MAX_CONTEXT_ITEM_CHARS``. ``pr_details`` is left alone: the SHA pin reads it.
    """
    logs = [_bounded_value(v) for v in context.recent_logs[:MAX_RECENT_LOGS]]
    dropped_logs = len(context.recent_logs) - len(logs)
    if dropped_logs > 0:
        logs.append(f"[{dropped_logs} more log entries truncated]")
    metadata = context.service_metadata
    if metadata is not None:
        kept = {k: _bounded_value(v) for k, v in list(metadata.items())[:MAX_METADATA_KEYS]}
        dropped_keys = len(metadata) - len(kept)
        if dropped_keys > 0:
            kept["_truncated"] = f"{dropped_keys} more keys truncated"
        metadata = kept
    return context.model_copy(update={"recent_logs": logs, "service_metadata": metadata})


class IncidentAlert(BaseModel):
    """What the pipeline sees of an incident's alert (observed facts only)."""

    alert_id: str = UNKNOWN
    source: str | None = None
    dd_event_id: str | None = None
    title: str
    severity: str = UNKNOWN
    service: str = UNKNOWN
    tags: IncidentTags = Field(default_factory=IncidentTags)
    context: IncidentContext = Field(default_factory=IncidentContext)
    timestamp: datetime | None = None

    @property
    def pr_sha(self) -> str | None:
        """``context.pr_details.sha`` stripped; None when absent or blank."""
        pr = self.context.pr_details
        sha = pr.sha.strip() if pr is not None and pr.sha is not None else ""
        return sha or None

    @classmethod
    def from_payload(cls, payload: IncidentPayload) -> IncidentAlert:
        """The alert for *payload*, its context bounded before it reaches a prompt.

        NOT NULL columns with no source get ``"unknown"``.
        """
        return cls(
            alert_id=payload.alert_id or UNKNOWN,
            source=payload.source,
            dd_event_id=payload.dd_event_id,
            title=payload.title,
            severity=payload.severity,
            service=payload.tags.service or UNKNOWN,
            tags=payload.tags,
            context=bounded_context(payload.context),
            timestamp=payload.timestamp,
        )

    @classmethod
    def from_phase1(cls, alert: AlertPayload) -> IncidentAlert:
        """Adapt a Phase-1 ``AlertPayload`` (eval runner / scenario scripts)."""
        title = alert.metadata.get("title")
        if not (isinstance(title, str) and title.strip()):
            title = (
                f"{alert.metric} {alert.current_value} (threshold {alert.threshold}) "
                f"on {alert.service}"
            )
        return cls(
            alert_id=alert.alert_id,
            source=alert.source.value,
            title=title.strip(),
            severity=alert.severity.value,
            service=alert.service,
            tags=IncidentTags(service=alert.service),
            timestamp=alert.timestamp,
        )
