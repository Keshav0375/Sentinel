"""Tests for eval/rubric.py — dimensions, rubric content, system prompt, judge input."""

from __future__ import annotations

import json

from sentinel.eval.rubric import (
    DIMENSION_RUBRICS,
    JUDGE_SYSTEM_PROMPT,
    DimensionRubric,
    EvalDimension,
    format_judge_input,
)

# ── DimensionRubric structure ─────────────────────────────────────────────────


class TestDimensionRubricStructure:
    def test_all_six_dimensions_have_rubrics(self) -> None:
        assert len(DIMENSION_RUBRICS) == 6
        for dim in EvalDimension:
            assert dim in DIMENSION_RUBRICS

    def test_each_rubric_has_six_levels(self) -> None:
        for dim, rubric in DIMENSION_RUBRICS.items():
            assert len(rubric.levels) == 6, f"{dim} has {len(rubric.levels)} levels"

    def test_rubric_levels_span_0_to_5(self) -> None:
        for dim, rubric in DIMENSION_RUBRICS.items():
            assert set(rubric.levels.keys()) == {0, 1, 2, 3, 4, 5}, (
                f"{dim} missing some level keys"
            )

    def test_each_rubric_has_nonempty_description(self) -> None:
        for dim, rubric in DIMENSION_RUBRICS.items():
            assert len(rubric.description) > 10, f"{dim} description too short"

    def test_each_level_has_nonempty_text(self) -> None:
        for dim, rubric in DIMENSION_RUBRICS.items():
            for level, text in rubric.levels.items():
                assert len(text) > 5, f"{dim} level {level} text too short"

    def test_rubric_dimension_field_matches_key(self) -> None:
        for dim, rubric in DIMENSION_RUBRICS.items():
            assert rubric.dimension == dim

    def test_rubric_is_frozen_dataclass(self) -> None:
        rubric = DIMENSION_RUBRICS[EvalDimension.TRIAGE_ACCURACY]
        assert isinstance(rubric, DimensionRubric)
        try:
            rubric.description = "changed"  # type: ignore[misc]
            assert False, "Should have raised FrozenInstanceError"
        except Exception:
            pass  # expected — frozen dataclass


# ── Rubric content spot-checks ────────────────────────────────────────────────


class TestRubricContent:
    def test_triage_accuracy_5_mentions_service_and_severity(self) -> None:
        rubric = DIMENSION_RUBRICS[EvalDimension.TRIAGE_ACCURACY]
        text = rubric.levels[5].lower()
        assert "service" in text
        assert "severity" in text

    def test_remediation_safety_5_mentions_hitl(self) -> None:
        rubric = DIMENSION_RUBRICS[EvalDimension.REMEDIATION_SAFETY]
        assert "HITL" in rubric.levels[5]

    def test_remediation_safety_0_mentions_dangerous(self) -> None:
        rubric = DIMENSION_RUBRICS[EvalDimension.REMEDIATION_SAFETY]
        text = rubric.levels[0].lower()
        assert "dangerous" in text or "approval" in text

    def test_comms_quality_5_lists_sections(self) -> None:
        rubric = DIMENSION_RUBRICS[EvalDimension.COMMS_QUALITY]
        text = rubric.levels[5]
        assert "Impact" in text
        assert "Root Cause" in text

    def test_mttr_0_means_never_reached(self) -> None:
        rubric = DIMENSION_RUBRICS[EvalDimension.MTTR]
        text = rubric.levels[0].lower()
        assert "never" in text or "reach" in text

    def test_tool_efficiency_0_means_no_tools(self) -> None:
        rubric = DIMENSION_RUBRICS[EvalDimension.TOOL_EFFICIENCY]
        text = rubric.levels[0].lower()
        assert "no tools" in text or "called" in text


# ── JUDGE_SYSTEM_PROMPT ───────────────────────────────────────────────────────


class TestJudgeSystemPrompt:
    def test_prompt_is_nonempty(self) -> None:
        assert len(JUDGE_SYSTEM_PROMPT) > 100

    def test_prompt_contains_all_dimension_names(self) -> None:
        for dim in EvalDimension:
            assert str(dim) in JUDGE_SYSTEM_PROMPT, (
                f"Dimension '{dim}' missing from judge system prompt"
            )

    def test_prompt_mentions_0_to_5_scale(self) -> None:
        assert "0-5" in JUDGE_SYSTEM_PROMPT or "0 to 5" in JUDGE_SYSTEM_PROMPT.lower()

    def test_prompt_includes_json_output_format(self) -> None:
        assert "dimension_scores" in JUDGE_SYSTEM_PROMPT

    def test_prompt_mentions_reasoning(self) -> None:
        assert "reasoning" in JUDGE_SYSTEM_PROMPT.lower()

    def test_prompt_contains_rubric_levels(self) -> None:
        assert "### triage_accuracy" in JUDGE_SYSTEM_PROMPT
        assert "### root_cause_correctness" in JUDGE_SYSTEM_PROMPT

    def test_prompt_instructs_no_text_outside_json(self) -> None:
        lower = JUDGE_SYSTEM_PROMPT.lower()
        assert "only" in lower or "outside" in lower

    def test_prompt_references_ground_truth(self) -> None:
        assert "ground truth" in JUDGE_SYSTEM_PROMPT.lower()


# ── format_judge_input ────────────────────────────────────────────────────────


class TestFormatJudgeInput:
    def _sample_trajectory(self) -> dict:
        return {
            "incident_id": "inc-001",
            "scenario_id": "bad_deploy_01",
            "timeline": [
                {"agent": "triage_agent", "action": "get_service_metadata", "result": "ok"},
                {"agent": "log_analyst_agent", "action": "fetch_logs", "result": "errors found"},
            ],
            "triage_result": {"severity": "P1", "affected_service": "api-gateway"},
        }

    def _sample_ground_truth(self) -> dict:
        return {
            "severity": "P1",
            "affected_service": "api-gateway",
            "root_cause_summary": "Missing config key",
            "recommended_action": "rollback",
            "deploy_id": "deploy-abc123",
        }

    def test_returns_string(self) -> None:
        result = format_judge_input(self._sample_trajectory(), self._sample_ground_truth())
        assert isinstance(result, str)

    def test_contains_incident_trajectory_header(self) -> None:
        result = format_judge_input(self._sample_trajectory(), self._sample_ground_truth())
        assert "Incident Trajectory" in result

    def test_contains_ground_truth_header(self) -> None:
        result = format_judge_input(self._sample_trajectory(), self._sample_ground_truth())
        assert "Ground Truth" in result

    def test_trajectory_data_appears_in_output(self) -> None:
        result = format_judge_input(self._sample_trajectory(), self._sample_ground_truth())
        assert "inc-001" in result
        assert "bad_deploy_01" in result

    def test_ground_truth_data_appears_in_output(self) -> None:
        result = format_judge_input(self._sample_trajectory(), self._sample_ground_truth())
        assert "api-gateway" in result
        assert "deploy-abc123" in result

    def test_trajectory_is_valid_json_in_output(self) -> None:
        result = format_judge_input(self._sample_trajectory(), self._sample_ground_truth())
        # Extract JSON block from trajectory section
        start = result.index("```json\n") + 8
        end = result.index("\n```", start)
        block = result[start:end]
        parsed = json.loads(block)
        assert parsed["incident_id"] == "inc-001"

    def test_empty_trajectory_does_not_raise(self) -> None:
        result = format_judge_input({}, {})
        assert isinstance(result, str)
        assert "Ground Truth" in result

    def test_non_serializable_values_handled(self) -> None:
        from datetime import datetime

        traj = {"ts": datetime(2026, 5, 13)}
        result = format_judge_input(traj, {})
        assert isinstance(result, str)
