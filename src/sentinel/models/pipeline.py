"""Pipeline models — what the orchestrator takes (PipelineInput) and returns (PipelineResult).

``PipelineInput`` is the phase-4 contract between the webhook and the orchestrator
(decision 2026-10-15): the incident id, the alert, the §3.1 ``signal_type`` the plan step
reads, and the correlation id carried from the Event Grid bridge (§2.1). Phase 5 owns the
real ``POST /webhooks/incident`` body; until then :meth:`PipelineInput.from_alert` derives
the two new fields from the alert's ``metadata`` (see there).

``PipelineResult`` is the terminal outcome: ``rollback_proposed`` with the rollback spec
GitHub Actions turns into a revert PR, or ``escalated`` with the context a human needs.
Nothing here executes anything. The §9.3 job outputs (``resolution_type``, ``root_cause``,
``confidence``, ``target_deploy``, ``evidence_summary``) are all in its ``model_dump``: the
last two (and ``target_sha``) are computed fields, so they reach ``pipeline_result``, short-
term memory and ``GET /incidents/{id}`` with the rest.
"""

from __future__ import annotations

import uuid
from typing import Any, Final, Literal, Self, get_args

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from sentinel.models.alert import AlertPayload
from sentinel.models.tools import EscalationContext, RollbackSpec

__all__ = [
    "DEFAULT_SIGNAL_TYPE",
    "SIGNAL_TYPES",
    "PipelineInput",
    "PipelineResult",
    "PipelineStatus",
    "ResolutionType",
    "SignalType",
]

# §3.1: deploy_failure (case ii, rollback fast path) | runtime_error (case iii, full loop).
SignalType = Literal["deploy_failure", "runtime_error"]
SIGNAL_TYPES: Final[tuple[SignalType, ...]] = get_args(SignalType)
# An alert that does not say which case it is runs the full pipeline.
DEFAULT_SIGNAL_TYPE: Final[SignalType] = "runtime_error"

# Terminal statuses (decision 2026-10-15); "resolved" is reserved for a later
# "revert deploy succeeded" signal.
PipelineStatus = Literal["rollback_proposed", "escalated"]
ResolutionType = Literal["rollback", "escalated"]


def _uuid_str(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a UUID") from exc


class PipelineInput(BaseModel):
    """One incident for the orchestrator to run."""

    incident_id: str = Field(description="Incident id: a UUID string (incidents.id, §5.3).")
    alert: AlertPayload
    signal_type: SignalType = Field(description="Which §3.1 case the incident is.")
    correlation_id: str = Field(description="The bridge's correlation UUID (§2.1).")

    @field_validator("incident_id", "correlation_id")
    @classmethod
    def _is_uuid(cls, value: str) -> str:
        return _uuid_str(value)

    @classmethod
    def from_alert(cls, incident_id: str, alert: AlertPayload) -> PipelineInput:
        """The input for *alert*, deriving the two fields today's alert does not carry.

        Until phase 5 lands the ``POST /webhooks/incident`` body (§3.1), the derivation is:

        - ``signal_type``: ``alert.metadata["signal_type"]`` when it is ``deploy_failure``
          or ``runtime_error`` (any case), else :data:`DEFAULT_SIGNAL_TYPE` (the full
          pipeline — never the fast path by accident).
        - ``correlation_id``: ``alert.metadata["correlation_id"]`` when it is a UUID, else a
          fresh ``uuid4`` (the bridge generates it in §2; a direct POST has none).
        """
        metadata = alert.metadata
        return cls(
            incident_id=incident_id,
            alert=alert,
            signal_type=_signal_type(metadata.get("signal_type")),
            correlation_id=_correlation_id(metadata.get("correlation_id")),
        )


def _signal_type(value: Any) -> SignalType:
    if isinstance(value, str):
        for signal in SIGNAL_TYPES:
            if value.strip().lower() == signal:
                return signal
    return DEFAULT_SIGNAL_TYPE


def _correlation_id(value: Any) -> str:
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            pass
    return str(uuid.uuid4())


class PipelineResult(BaseModel):
    """The outcome of one pipeline run (§4.2), returned whether or not anything failed.

    Invariants (validated): ``rollback_proposed`` ⇔ ``resolution_type == "rollback"`` ⇔ a
    ``rollback_spec`` and no ``escalation``; ``escalated`` ⇔ an ``escalation`` and no spec.
    """

    incident_id: str
    status: PipelineStatus
    resolution_type: ResolutionType
    root_cause: str = Field(description="The rollback spec's root cause, or the hypothesis.")
    confidence: float = Field(ge=0.0, le=1.0)
    rollback_spec: RollbackSpec | None = None
    escalation: EscalationContext | None = None
    reflexion_loops: int = Field(ge=0, description="Loop-backs from reflexion to analysis.")
    parse_failures: int = Field(
        default=0, ge=0, description="Agent replies that did not parse as their output model."
    )
    tool_calls_used: int = Field(ge=0, description="Tool calls run, counted in code (§4.6).")
    tokens_used: int = Field(ge=0, description="Input + output tokens of every LLM call.")
    models_used: list[str] = Field(
        default=[], description="Models that produced a reply, in first-use order."
    )

    @model_validator(mode="after")
    def _one_outcome(self) -> Self:
        if self.status == "rollback_proposed":
            if self.resolution_type != "rollback" or self.rollback_spec is None:
                raise ValueError("rollback_proposed needs resolution_type rollback and a spec")
            if self.escalation is not None:
                raise ValueError("rollback_proposed carries no escalation")
        else:
            if self.resolution_type != "escalated" or self.escalation is None:
                raise ValueError("escalated needs resolution_type escalated and an escalation")
            if self.rollback_spec is not None:
                raise ValueError("escalated carries no rollback spec")
        return self

    # Computed fields: serialized by model_dump (and ignored when a dump is validated back).
    @computed_field  # type: ignore[prop-decorator]
    @property
    def target_deploy(self) -> str | None:
        """§9.3 ``target_deploy``: the commit the revert PR reverts (GHA's TARGET_SHA)."""
        return None if self.rollback_spec is None else self.rollback_spec.target_deploy.commit_sha

    @computed_field  # type: ignore[prop-decorator]
    @property
    def target_sha(self) -> str | None:
        """The same commit under the name the incident workflow uses (TARGET_SHA)."""
        return self.target_deploy

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evidence_summary(self) -> str:
        """§9.3 ``evidence_summary``: the spec's, or the escalation's evidence joined."""
        if self.rollback_spec is not None:
            return self.rollback_spec.evidence_summary
        assert self.escalation is not None  # the validator guarantees one outcome
        return "; ".join(self.escalation.evidence)
