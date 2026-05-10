"""Tests for sentinel.models.eval_result."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinel.models.eval_result import DimensionScore, EvalDimension, TrajectoryScore

# ── EvalDimension ─────────────────────────────────────────────────────────────


def test_eval_dimension_values() -> None:
    assert EvalDimension.TRIAGE_ACCURACY == "triage_accuracy"
    assert EvalDimension.ROOT_CAUSE_CORRECTNESS == "root_cause_correctness"
    assert EvalDimension.TOOL_EFFICIENCY == "tool_efficiency"
    assert EvalDimension.MTTR == "mttr"
    assert EvalDimension.REMEDIATION_SAFETY == "remediation_safety"
    assert EvalDimension.COMMS_QUALITY == "comms_quality"


def test_eval_dimension_count() -> None:
    assert len(EvalDimension) == 6


# ── DimensionScore ────────────────────────────────────────────────────────────


def test_dimension_score_valid() -> None:
    ds = DimensionScore(
        dimension=EvalDimension.TRIAGE_ACCURACY,
        score=4.5,
        reasoning="Correctly identified P1 severity and api-gateway as affected service",
    )
    assert ds.dimension is EvalDimension.TRIAGE_ACCURACY
    assert ds.score == 4.5


def test_dimension_score_zero() -> None:
    ds = DimensionScore(
        dimension=EvalDimension.MTTR,
        score=0.0,
        reasoning="Agent timed out before producing a remediation plan",
    )
    assert ds.score == 0.0


def test_dimension_score_max() -> None:
    ds = DimensionScore(
        dimension=EvalDimension.COMMS_QUALITY,
        score=5.0,
        reasoning="Perfect format, all sections present, clear language",
    )
    assert ds.score == 5.0


def test_dimension_score_below_zero_raises() -> None:
    with pytest.raises(ValidationError):
        DimensionScore(
            dimension=EvalDimension.MTTR,
            score=-1.0,
            reasoning="invalid",
        )


def test_dimension_score_above_five_raises() -> None:
    with pytest.raises(ValidationError):
        DimensionScore(
            dimension=EvalDimension.MTTR,
            score=5.1,
            reasoning="invalid",
        )


# ── TrajectoryScore ───────────────────────────────────────────────────────────


def _sample_scores() -> list[DimensionScore]:
    return [
        DimensionScore(
            dimension=EvalDimension.TRIAGE_ACCURACY, score=4.0, reasoning="good"
        ),
        DimensionScore(
            dimension=EvalDimension.ROOT_CAUSE_CORRECTNESS, score=5.0, reasoning="perfect"
        ),
        DimensionScore(
            dimension=EvalDimension.TOOL_EFFICIENCY, score=3.5, reasoning="ok"
        ),
        DimensionScore(
            dimension=EvalDimension.MTTR, score=4.0, reasoning="fast"
        ),
        DimensionScore(
            dimension=EvalDimension.REMEDIATION_SAFETY, score=5.0, reasoning="always gated"
        ),
        DimensionScore(
            dimension=EvalDimension.COMMS_QUALITY, score=3.0, reasoning="decent"
        ),
    ]


def test_trajectory_score_full() -> None:
    scores = _sample_scores()
    total = sum(s.score for s in scores) / len(scores)
    ts = TrajectoryScore(
        incident_id="inc-001",
        scenario_id="bad_deploy_01",
        dimension_scores=scores,
        total_score=total,
        judge_model="llama-3.1-8b-instant",
    )
    assert ts.incident_id == "inc-001"
    assert ts.scenario_id == "bad_deploy_01"
    assert len(ts.dimension_scores) == 6
    assert ts.total_score == pytest.approx(4.083, abs=0.01)
    assert ts.judge_model == "llama-3.1-8b-instant"


def test_trajectory_score_defaults() -> None:
    ts = TrajectoryScore(
        incident_id="inc-x",
        scenario_id="test_01",
        judge_model="test-model",
    )
    assert ts.dimension_scores == []
    assert ts.total_score == 0.0


def test_trajectory_score_total_out_of_range_raises() -> None:
    with pytest.raises(ValidationError):
        TrajectoryScore(
            incident_id="x",
            scenario_id="y",
            total_score=6.0,
            judge_model="m",
        )


def test_trajectory_score_json_round_trip() -> None:
    scores = _sample_scores()
    ts = TrajectoryScore(
        incident_id="inc-rt",
        scenario_id="scenario-rt",
        dimension_scores=scores,
        total_score=4.0,
        judge_model="llama-3.1-8b-instant",
    )
    reloaded = TrajectoryScore.model_validate_json(ts.model_dump_json())
    assert reloaded.incident_id == ts.incident_id
    assert len(reloaded.dimension_scores) == 6
    assert reloaded.dimension_scores[0].dimension is EvalDimension.TRIAGE_ACCURACY
