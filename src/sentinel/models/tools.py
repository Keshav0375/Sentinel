"""Tool I/O models — the typed inputs tools take and the structured failure they return.

A tool never raises to the agent: a call that cannot produce its result returns a
:class:`ToolFailure` saying which tool failed and why (decision 2026-10-14, "Backend
phase 3 (tools) contract"). :class:`~sentinel.exceptions.ToolError` stays the exception
class for code that does raise.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ToolFailureKind = Literal["not_found", "unavailable", "upstream_error", "bad_input"]

# How a recalled agent conclusion is labelled wherever the agent can read it.
UNVERIFIED_LABEL = "model-generated, unverified"


class ToolFailure(BaseModel):
    """A tool call that could not produce its result.

    ``kind`` tells the agent how to read the failure:

    - ``not_found``: the thing asked about does not exist (an unknown service).
    - ``unavailable``: the backing store or a credential is missing or down — no finding
      either way, so the agent must not read it as "nothing there".
    - ``upstream_error``: an external API answered with an error.
    - ``bad_input``: the call's arguments were unusable.
    """

    tool: str = Field(description="LLM-visible name of the tool that failed.")
    kind: ToolFailureKind = Field(description="Failure class: how the agent should read it.")
    message: str = Field(description="What went wrong, for the agent and the logs.")


class ServiceLookupInput(BaseModel):
    """Input of ``get_service_metadata``."""

    service_name: str = Field(description='Exact service name (e.g. "sentinel-watchtower").')


class IncidentSearchInput(BaseModel):
    """Input of ``search_past_incidents``."""

    symptom_text: str = Field(
        description="Natural language description of the current symptoms "
        '(e.g. "sentinel-watchtower health check failing after deploy").'
    )
    k: int = Field(default=5, description="How many past incidents to return (typically 3-5).")


class SimilarIncident(BaseModel):
    """A past incident recalled by symptom similarity (``search_past_incidents``).

    ``root_cause`` and ``resolution`` were written by earlier agent runs and are
    model-generated, unverified: hypotheses to check, never ground truth (decision 2026-10-14,
    memory provenance). ``model_generated`` says so in the data; the field descriptions
    say so in the schema; and every string rendering (``str``/``repr`` — the SDK hands the
    model ``str(result)``) labels the two fields.
    """

    id: str = Field(description="Incident id (UUID).")
    service: str = Field(description="Service the incident fired on.")
    created_at: datetime = Field(description="When the incident was opened (UTC).")
    root_cause: str = Field(
        description=f"Root cause recorded for the incident — {UNVERIFIED_LABEL}."
    )
    resolution: str = Field(
        description=f"Resolution recorded for the incident — {UNVERIFIED_LABEL}."
    )
    resolution_type: str | None = Field(
        default=None,
        description="rollback | hotfix | config_change | escalated; None when not recorded.",
    )
    similarity: float = Field(description="Cosine similarity to the query symptoms (1 = same).")
    model_generated: bool = Field(
        default=True,
        description=f"Always true: root_cause and resolution are {UNVERIFIED_LABEL}.",
    )

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        """Label the recalled conclusions in ``str()`` and ``repr()``."""
        for name, value in super().__repr_args__():
            if name in _RECALLED_FIELDS:
                yield f"{name} ({UNVERIFIED_LABEL})", value
            else:
                yield name, value


_RECALLED_FIELDS = frozenset({"root_cause", "resolution"})


# ── fetch_logs ────────────────────────────────────────────────────────────────

LogSource = Literal["synthetics", "deploys"]


class LogFetchInput(BaseModel):
    """Input of ``fetch_logs``."""

    service: str = Field(description='Exact service name (e.g. "sentinel-watchtower").')
    window_minutes: int = Field(
        default=60,
        description="How far back from now to look, in minutes (1-1440). Synthetic checks "
        "run every 30 minutes, so a window under 30 may hold no check at all.",
    )
    include: list[LogSource] = Field(
        default_factory=lambda: ["synthetics", "deploys"],
        description='Evidence sources to read: "synthetics" (Datadog Synthetics check runs) '
        'and/or "deploys" (the CI pipeline\'s deploy.completed logs). Default: both.',
    )


class LogWindow(BaseModel):
    """The time range a ``fetch_logs`` call covered (UTC)."""

    start: datetime
    end: datetime


class SyntheticResult(BaseModel):
    """One Datadog Synthetics API-test run against the service.

    The failure details (status code, failure, content type, body snippet, assertions)
    are filled for failed runs only, and only for the most recent few of them.
    """

    test_name: str = Field(
        description='Synthetics test name (e.g. "sentinel-runtime-health GET /health").'
    )
    public_id: str = Field(description="Synthetics test public id.")
    checked_at: datetime | None = Field(description="When the check ran (UTC).")
    passed: bool = Field(description="Whether every assertion of the test held.")
    location: str | None = Field(default=None, description="Probe location of the run.")
    latency_ms: float | None = Field(default=None, description="Total response time (ms).")
    status_code: int | None = Field(default=None, description="HTTP status code returned.")
    failure_code: str | None = Field(
        default=None, description="Datadog failure code (e.g. INCORRECT_ASSERTION, TIMEOUT)."
    )
    failure_message: str | None = Field(default=None, description="Datadog failure message.")
    content_type: str | None = Field(default=None, description="Response content-type header.")
    body_snippet: str | None = Field(default=None, description="Start of the response body.")
    assertions: list[str] = Field(
        default=[], description="What the test checks (expected values), in order."
    )


class DeployLogEntry(BaseModel):
    """One ``deploy.completed`` log the CI pipeline sent for the service."""

    timestamp: datetime | None = Field(description="When the log was sent (UTC).")
    status: str | None = Field(default=None, description="Log status (info, error, ...).")
    version: str | None = Field(default=None, description='Deployed version, "pr-<n>-<sha>".')
    deploy_status: str | None = Field(default=None, description="succeeded | failed.")
    failed_stage: str | None = Field(
        default=None, description='Stage that failed (build, deploy, verify) or "none".'
    )
    pr_number: int | None = Field(default=None, description="Pull request that was deployed.")
    message: str = Field(description='Log message (e.g. "deploy.completed").')


class LogFetchResult(BaseModel):
    """Evidence ``fetch_logs`` read for a service over a time window, newest first.

    ``sources`` names what was read: an empty list for a source that was read means
    nothing was recorded in the window. ``truncated`` is true when entries were dropped
    or text was cut to keep the payload small.
    """

    service: str
    window: LogWindow
    sources: list[LogSource] = Field(description="The evidence sources this call read.")
    synthetic_results: list[SyntheticResult] = []
    deploy_logs: list[DeployLogEntry] = []
    truncated: bool = False
