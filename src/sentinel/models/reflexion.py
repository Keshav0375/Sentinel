"""Reflexion models — the critique of an Analysis hypothesis (backend §4.3).

Reflexion is a step of the orchestrator's loop, not a free agent: the ``reflexion`` prompt
reads the Analysis hypothesis and its evidence and answers with exactly one
:class:`ReflexionResult` as JSON. Below :data:`REFLEXION_CONFIDENCE_THRESHOLD` it names the
one analysis-tool call (``fetch_logs`` or ``get_deploy_details``) that would raise
confidence; the orchestrator passes that to the Analysis agent as guidance. Reflexion
calls no tool itself, and nothing here acts on the world.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal, Self, cast

from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from sentinel.models.tools import DeployQuery, LogFetchInput

__all__ = [
    "ANALYSIS_TOOLS",
    "REFLEXION_CONFIDENCE_THRESHOLD",
    "DeployDetailsCall",
    "FetchLogsCall",
    "ReflexionResult",
    "SuspectDeploy",
]

# §4.3: below this, reflexion must say which tool call to make next.
REFLEXION_CONFIDENCE_THRESHOLD = 0.7

# The only tools a next_tool_call may name — the Analysis agent's (decision 2026-10-15).
ANALYSIS_TOOLS: tuple[str, ...] = ("fetch_logs", "get_deploy_details")


class FetchLogsCall(BaseModel):
    """A ``fetch_logs`` call for the Analysis agent to make next."""

    tool: Literal["fetch_logs"] = Field(description='Always "fetch_logs".')
    args: LogFetchInput = Field(description="The fetch_logs arguments.")


class DeployDetailsCall(BaseModel):
    """A ``get_deploy_details`` call for the Analysis agent to make next."""

    tool: Literal["get_deploy_details"] = Field(description='Always "get_deploy_details".')
    args: DeployQuery = Field(description="The get_deploy_details arguments.")


class SuspectDeploy(BaseModel):
    """The deploy the hypothesis blames, copied from a ``get_deploy_details`` row."""

    commit_sha: str = Field(description="Deployed commit, copied exactly from get_deploy_details.")
    pr_number: int | None = Field(
        description="Pull request that was deployed, copied from get_deploy_details; "
        "null for a direct push."
    )

    @field_validator("commit_sha")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("commit_sha must not be blank")
        return value


class ReflexionResult(BaseModel):
    """Output of the ``reflexion`` prompt: the critique the orchestrator acts on.

    The schema (what the model is asked for) has every field required. Validation is
    tolerant, but a repair only ever makes the decision gate stricter, never looser:

    - ``confidence`` must be in [0.0, 1.0]: anything else (85 meaning 85 %, -1, NaN) is not
      repaired but rejected, so the reply counts as unparsed and never reaches the gate.
    - ``specific_deploy_identified`` is the model's own ``true`` AND a deploy that parsed.
      An explicit (or omitted) false always wins — a deploy never sets it — and true with
      no deploy is repaired to false.
    - A ``deploy`` or ``next_tool_call`` that does not parse (a blank SHA, a tool that is
      not an analysis tool) is dropped to null; an omitted list is empty.
    - ``next_tool_call`` is kept only below :data:`REFLEXION_CONFIDENCE_THRESHOLD`; it may
      be null there (the orchestrator then loops without guidance or escalates).
    """

    confidence: float = Field(
        description="Confidence in the hypothesis, 0.0 (none) to 1.0 (certain)."
    )
    supporting_evidence: list[str] = Field(
        description="Evidence items that support the hypothesis, each citing its tool result."
    )
    missing_or_contradictory: list[str] = Field(
        description="Evidence that is missing, or that contradicts the hypothesis."
    )
    additional_data_needed: list[str] = Field(
        description="Data that would raise confidence (service, source, time window)."
    )
    specific_deploy_identified: bool = Field(
        description="True only when the evidence points at one specific deploy."
    )
    deploy: SuspectDeploy | None = Field(
        description="The blamed deploy when specific_deploy_identified is true, else null."
    )
    next_tool_call: FetchLogsCall | DeployDetailsCall | None = Field(
        description="When confidence < 0.7: the one analysis-tool call to make next. "
        "Null otherwise."
    )

    @model_validator(mode="before")
    @classmethod
    def _repair(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        reply = dict(cast(dict[str, Any], data))
        for name in _LIST_FIELDS:
            if reply.get(name) is None:
                reply[name] = []
        reply["deploy"] = _parsed_or_none(_DEPLOY, reply.get("deploy"))
        reply["next_tool_call"] = _parsed_or_none(_NEXT_CALL, reply.get("next_tool_call"))
        claimed = reply.get("specific_deploy_identified") is True
        reply["specific_deploy_identified"] = claimed and reply["deploy"] is not None
        return reply

    @field_validator("confidence")
    @classmethod
    def _in_unit_interval(cls, value: float) -> float:
        if math.isnan(value) or not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.needs_more_evidence:
            self.next_tool_call = None
        return self

    @classmethod
    def is_explicit_reply(cls, data: Mapping[str, Any]) -> bool:
        """Whether *data* states every gate-relevant key itself: ``confidence`` and
        ``specific_deploy_identified``, and the ``deploy`` key when that is true (a null
        deploy is then repaired to not identified, as in any reply).

        ``coerce_output`` requires it of an object found inside a reply (not of a whole
        reply): validation fills defaults, so a quoted ``{"confidence": 0.95}`` would
        otherwise parse as a critique.
        """
        if "confidence" not in data or "specific_deploy_identified" not in data:
            return False
        return data["specific_deploy_identified"] is not True or "deploy" in data

    @property
    def needs_more_evidence(self) -> bool:
        """True below the §4.3 threshold: the orchestrator may loop back to Analysis."""
        return self.confidence < REFLEXION_CONFIDENCE_THRESHOLD


_LIST_FIELDS: tuple[str, ...] = (
    "supporting_evidence",
    "missing_or_contradictory",
    "additional_data_needed",
)
_DEPLOY: TypeAdapter[SuspectDeploy] = TypeAdapter(SuspectDeploy)
_NEXT_CALL: TypeAdapter[FetchLogsCall | DeployDetailsCall] = TypeAdapter(
    FetchLogsCall | DeployDetailsCall
)


def _parsed_or_none(adapter: TypeAdapter[Any], value: Any) -> Any:
    """*value* validated by *adapter*, or None when it is absent or does not parse."""
    if value is None:
        return None
    try:
        return adapter.validate_python(value)
    except ValidationError:
        return None
