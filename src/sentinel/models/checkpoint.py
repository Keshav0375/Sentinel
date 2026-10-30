"""Checkpoint models — one pipeline run's snapshot, and the ``incident_checkpoints`` row.

A :class:`RunSnapshot` is everything the orchestrator needs to resume an incident after a
restart (decision 2026-10-25 B): the request, what was decided so far, and every counter a
cap is enforced on, so a resumed run continues them instead of starting over. Each
checkpoint row holds a full snapshot (resume = restore + jump to the stage after it).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, JsonValue

from sentinel.models.analysis import AnalysisResult
from sentinel.models.incident import TimelineEntry, TriageResult
from sentinel.models.pipeline import PipelineInput
from sentinel.models.reflexion import ReflexionResult
from sentinel.models.tools import EscalationContext, RollbackSpec

__all__ = ["Checkpoint", "CheckpointStage", "RunSnapshot"]

# The stage a checkpoint was written after (``resumed`` is the marker of a resume attempt).
CheckpointStage = Literal[
    "accepted", "triage", "analysis", "reflexion", "resolution", "verify", "resumed"
]


class RunSnapshot(BaseModel):
    """One pipeline run's state after *stage* — what a resume restores."""

    request: PipelineInput
    started_at: datetime
    stage: CheckpointStage
    # The absolute wall-clock deadline (started_at + the bound at the first start); None in
    # a snapshot written before it was persisted (the resume derives it from started_at).
    deadline_at: datetime | None = None
    timeline: list[TimelineEntry]
    models_used: list[str]
    reflexion_loops: int
    parse_failures: int
    unparsed_critiques: int
    # Raised on every tool call (CheckpointWriter.save_budget), not only per stage.
    tool_calls_used: int
    tool_calls_refused: int
    escalation_drafted: bool
    unparsed_hypothesis: bool
    fallback_evidence: bool
    triage: TriageResult | None
    analysis: AnalysisResult | None
    critique: ReflexionResult | None
    guidance: ReflexionResult | None
    evidence: list[tuple[str, str]]
    gathered: list[tuple[str, str]]
    # The reflexion loop finished (the critique stands).
    loop_done: bool = False
    # The resolution attempt (0 or 1) and the reverification correction it runs with.
    attempt: int = 0
    correction: str | None = None
    # The resolution agent's raw draft (after a ``resolution`` checkpoint), else None.
    draft: RollbackSpec | EscalationContext | None = None


class Checkpoint(BaseModel):
    """One ``incident_checkpoints`` row."""

    incident_id: str
    step: int
    stage: CheckpointStage
    state: dict[str, JsonValue]
    created_at: datetime

    def snapshot(self) -> RunSnapshot:
        """The run snapshot this row holds.

        Raises:
            pydantic.ValidationError: The state is not a snapshot (a ``resumed`` marker).
        """
        return RunSnapshot.model_validate(self.state)
