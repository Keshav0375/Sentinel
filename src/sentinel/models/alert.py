"""Alert models — the ``POST /webhooks/incident`` body and what the pipeline sees of it.

``IncidentPayload`` is the §3.1 request body (with its tag / PR / context sub-models);
``IncidentAlert`` is the pipeline-facing alert built from it (no raw payload, signal type or
correlation id: those travel on ``PipelineInput``).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Final, Literal, get_args

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
# The eval label's shape: a PR head ref a dataset item may carry (decision 2026-10-25).
BRANCH_PATTERN: Final = re.compile(r"[A-Za-z0-9._/-]{1,100}")


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
    # The PR's head ref (the scenario branch, e.g. deployfail/03); the eval dataset item's label.
    # Eval metadata only: never shown to an agent (``IncidentAlert.agent_view``).
    branch: str | None = None
    files_changed: list[str] = []

    @field_validator("branch")
    @classmethod
    def _branch_shape(cls, value: str | None) -> str | None:
        """The stripped branch when it fits BRANCH_PATTERN, else None (dropped, never rejected)."""
        if value is None:
            return None
        value = value.strip()
        return value if BRANCH_PATTERN.fullmatch(value) else None


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

    def agent_view(self) -> IncidentAlert:
        """This alert as an agent sees it: ``context.pr_details.branch`` removed.

        The branch is the eval ground-truth label (decision 2026-10-25, amended after review):
        it labels the dataset item and never reaches a prompt.
        """
        pr = self.context.pr_details
        if pr is None or pr.branch is None:
            return self
        pr_view = pr.model_copy(update={"branch": None})
        context = self.context.model_copy(update={"pr_details": pr_view})
        return self.model_copy(update={"context": context})
