"""Eval models — EvalDimension, DimensionScore, TrajectoryScore."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, field_validator


class EvalDimension(StrEnum):
    """The six dimensions scored by the LLM-as-judge.

    Each dimension is scored 0-5 with reasoning. Together they capture both
    the correctness of the agent's conclusions and the quality of the process
    it followed to reach them.
    """

    TRIAGE_ACCURACY = "triage_accuracy"
    ROOT_CAUSE_CORRECTNESS = "root_cause_correctness"
    TOOL_EFFICIENCY = "tool_efficiency"
    MTTR = "mttr"
    REMEDIATION_SAFETY = "remediation_safety"
    COMMS_QUALITY = "comms_quality"


class DimensionScore(BaseModel):
    """A single dimension's score from the judge.

    score is on a 0-5 integer/float scale (0 = completely wrong, 5 = perfect).
    reasoning is the judge's explanation for the score — crucial for debugging
    agent behavior and improving system prompts.
    """

    dimension: EvalDimension
    score: float
    reasoning: str

    @field_validator("score")
    @classmethod
    def score_in_range(cls, v: float) -> float:
        if v < 0.0 or v > 5.0:
            raise ValueError("score must be between 0 and 5")
        return v


class TrajectoryScore(BaseModel):
    """Full eval result for one incident trajectory.

    Produced by the LLM-as-judge after reviewing the complete timeline
    (every tool call, handoff, and agent output) against the scenario's
    known ground truth.

    total_score is the mean of all dimension scores (0-5 scale).
    """

    incident_id: str
    scenario_id: str
    dimension_scores: list[DimensionScore] = []
    total_score: float = 0.0
    judge_model: str

    @field_validator("total_score")
    @classmethod
    def total_score_in_range(cls, v: float) -> float:
        if v < 0.0 or v > 5.0:
            raise ValueError("total_score must be between 0 and 5")
        return v
