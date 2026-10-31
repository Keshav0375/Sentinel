"""Eval models — EvalDimension, DimensionScore, TrajectoryScore, GroundTruth*."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, JsonValue, field_validator, model_validator

# The eval score scale (decision 2026-10-22): every dimension and total is in [0, 1].
SCORE_MIN: Final = 0.0
SCORE_MAX: Final = 1.0


class EvalDimension(StrEnum):
    """The five dimensions scored by the LLM-as-judge.

    Each dimension is scored 0-1 with reasoning. Together they capture both
    the correctness of the agent's conclusions and the quality of the process
    it followed to reach them.
    """

    TRIAGE_ACCURACY = "triage_accuracy"
    ROOT_CAUSE_CORRECTNESS = "root_cause_correctness"
    TOOL_EFFICIENCY = "tool_efficiency"
    MTTR = "mttr"
    REMEDIATION_SAFETY = "remediation_safety"


class DimensionScore(BaseModel):
    """A single dimension's score from the judge.

    score is on a 0-1 scale (0 = completely wrong, 1 = perfect).
    reasoning is the judge's explanation for the score — crucial for debugging
    agent behavior and improving system prompts.
    """

    dimension: EvalDimension
    score: float
    reasoning: str

    @field_validator("score")
    @classmethod
    def score_in_range(cls, v: float) -> float:
        if v < SCORE_MIN or v > SCORE_MAX:
            raise ValueError("score must be between 0 and 1")
        return v


class TrajectoryScore(BaseModel):
    """Full eval result for one incident trajectory.

    Produced by the LLM-as-judge after reviewing the complete timeline
    (every tool call, handoff, and agent output) against the scenario's
    known ground truth.

    total_score is the mean of all dimension scores (0-1 scale).

    judge_conflict marks a run the judge would have graded itself on — its model is in the
    run's ``models_used`` (decision 2026-10-18). Such a score is invalid: it is zeroed,
    never judged, and excluded from every aggregate.

    judge_failed marks a score the judge could not produce (every retry failed). It is zeroed,
    stored flagged, never pushed and excluded from every aggregate.
    """

    incident_id: str
    scenario_id: str
    dimension_scores: list[DimensionScore] = []
    total_score: float = 0.0
    judge_model: str
    judge_conflict: bool = False
    judge_failed: bool = False

    @field_validator("total_score")
    @classmethod
    def total_score_in_range(cls, v: float) -> float:
        if v < SCORE_MIN or v > SCORE_MAX:
            raise ValueError("total_score must be between 0 and 1")
        return v

    def score_for(self, dimension: EvalDimension) -> float:
        """The score of *dimension*; 0.0 when it is absent (the judge's missing-dimension rule)."""
        for ds in self.dimension_scores:
            if ds.dimension == dimension:
                return ds.score
        return 0.0

    @property
    def triage_accuracy(self) -> float:
        """§6.4 attribute style; not serialised."""
        return self.score_for(EvalDimension.TRIAGE_ACCURACY)

    @property
    def root_cause_correctness(self) -> float:
        """§6.4 attribute style; not serialised."""
        return self.score_for(EvalDimension.ROOT_CAUSE_CORRECTNESS)

    @property
    def tool_efficiency(self) -> float:
        """§6.4 attribute style; not serialised."""
        return self.score_for(EvalDimension.TOOL_EFFICIENCY)

    @property
    def mttr(self) -> float:
        """§6.4 attribute style; not serialised."""
        return self.score_for(EvalDimension.MTTR)

    @property
    def remediation_safety(self) -> float:
        """§6.4 attribute style; not serialised."""
        return self.score_for(EvalDimension.REMEDIATION_SAFETY)

    def to_eval_score(self) -> dict[str, JsonValue]:
        """The ``incidents.eval_score`` JSONB (decision 2026-10-22, amended)."""
        scores: dict[str, JsonValue] = {str(ds.dimension): ds.score for ds in self.dimension_scores}
        reasoning: dict[str, JsonValue] = {
            str(ds.dimension): ds.reasoning for ds in self.dimension_scores
        }
        return {
            "judge_model": self.judge_model,
            "judge_conflict": self.judge_conflict,
            "judge_failed": self.judge_failed,
            "scores": scores,
            "reasoning": reasoning,
        }


class GroundTruthCheck(StrEnum):
    """The four scenarios/branches.yaml checks (decision 2026-10-22): 1 = pass, 0 = fail."""

    FIRED = "fired"
    CLASSIFIED = "classified"
    CORRELATED = "correlated"
    REVERT_TARGET = "revert_target"


class GroundTruthResult(BaseModel):
    """One dataset item's ground-truth checks: every GroundTruthCheck, each 0.0 or 1.0.

    judge_conflict items (decision 2026-10-18) are reported but excluded from aggregates.

    incident_id is the judged (primary) incident, None when nothing was judged; judge_failed
    mirrors the judge score's flag; created_at is the LangFuse dataset item's creation time
    (UTC), None when unknown. ci_eval's eval.scored emitter selects the newest item per branch by
    it and joins results[] on incident_id (decision 2026-10-28).
    """

    item_id: str
    branch: str
    case: str
    checks: dict[GroundTruthCheck, float]
    judge_conflict: bool = False
    incident_id: str | None = None
    judge_failed: bool = False
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _all_checks_binary(self) -> GroundTruthResult:
        if set(self.checks) != set(GroundTruthCheck):
            raise ValueError("checks must hold every GroundTruthCheck")
        if any(v not in (SCORE_MIN, SCORE_MAX) for v in self.checks.values()):
            raise ValueError("a ground-truth check is 0.0 or 1.0")
        return self

    def score_for(self, check: GroundTruthCheck) -> float:
        """The value of *check*."""
        return self.checks[check]
