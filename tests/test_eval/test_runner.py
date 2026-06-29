"""Tests for eval/runner.py — batch eval runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sentinel.eval.runner import (
    EvalRunResult,
    _auto_approve,
    run_eval,
    run_scenario_eval,
    scores_from_results,
    summary_stats,
)
from sentinel.models.eval_result import DimensionScore, EvalDimension, TrajectoryScore

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_trajectory_score(total: float = 4.0) -> TrajectoryScore:
    return TrajectoryScore(
        incident_id="inc-001",
        scenario_id="bad_deploy_01",
        dimension_scores=[
            DimensionScore(dimension=d, score=total, reasoning="ok") for d in EvalDimension
        ],
        total_score=total,
        judge_model="test-model",
    )


def _make_eval_result(
    scenario_id: str = "bad_deploy_01",
    total: float = 4.0,
    pipeline_error: str | None = None,
    trajectory_missing: bool = False,
) -> EvalRunResult:
    return EvalRunResult(
        scenario_id=scenario_id,
        incident_id=f"eval-{scenario_id}-abc12345",
        score=_make_trajectory_score(total),
        pipeline_error=pipeline_error,
        trajectory_missing=trajectory_missing,
    )


def _make_scenario_mock(scenario_id: str = "bad_deploy_01") -> MagicMock:
    """Build a minimal Scenario mock with ground_truth."""
    gt = MagicMock()
    gt.model_dump.return_value = {
        "severity": "P1",
        "affected_service": "api-gateway",
        "root_cause_summary": "Missing config",
        "recommended_action": "rollback",
        "deploy_id": "deploy-abc123",
    }
    scenario = MagicMock()
    scenario.scenario_id = scenario_id
    scenario.ground_truth = gt
    return scenario


def _make_alert_mock() -> MagicMock:
    alert = MagicMock()
    alert.alert_id = "alert-abcdef12"
    alert.model_dump_json.return_value = '{"service": "api-gateway"}'
    return alert


def _make_stm_mock() -> MagicMock:
    stm = MagicMock()
    stm.get_context.return_value = {
        "status": "resolved",
        "alert": MagicMock(service="api-gateway"),
        "timeline": [],
    }
    return stm


def _make_groq_client() -> MagicMock:
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock()
    return client


# ── _auto_approve ─────────────────────────────────────────────────────────────


class TestAutoApprove:
    async def test_returns_approve(self) -> None:
        decision, comment = await _auto_approve("Roll back deploy-abc123")
        assert decision == "approve"

    async def test_comment_is_string_or_none(self) -> None:
        _, comment = await _auto_approve("some action")
        assert comment is None or isinstance(comment, str)

    async def test_does_not_raise_on_empty_display(self) -> None:
        decision, _ = await _auto_approve("")
        assert decision == "approve"


# ── scores_from_results ───────────────────────────────────────────────────────


class TestScoresFromResults:
    def test_extracts_scores(self) -> None:
        results = [_make_eval_result("s1", 3.0), _make_eval_result("s2", 4.0)]
        scores = scores_from_results(results)
        assert len(scores) == 2
        assert all(isinstance(s, TrajectoryScore) for s in scores)

    def test_empty_list(self) -> None:
        assert scores_from_results([]) == []

    def test_order_preserved(self) -> None:
        results = [_make_eval_result("a", 1.0), _make_eval_result("b", 5.0)]
        scores = scores_from_results(results)
        assert scores[0].total_score == pytest.approx(1.0)
        assert scores[1].total_score == pytest.approx(5.0)


# ── summary_stats ─────────────────────────────────────────────────────────────


class TestSummaryStats:
    def test_empty_returns_zeros(self) -> None:
        stats = summary_stats([])
        assert stats["avg_total"] == pytest.approx(0.0)
        assert stats["pass_rate"] == pytest.approx(0.0)

    def test_avg_total_is_mean(self) -> None:
        results = [_make_eval_result("a", 2.0), _make_eval_result("b", 4.0)]
        stats = summary_stats(results)
        assert stats["avg_total"] == pytest.approx(3.0)

    def test_pass_rate_with_threshold_3(self) -> None:
        # 2.0 fails, 3.0 passes, 4.0 passes → 2/3 pass
        results = [
            _make_eval_result("a", 2.0),
            _make_eval_result("b", 3.0),
            _make_eval_result("c", 4.0),
        ]
        stats = summary_stats(results)
        assert stats["pass_rate"] == pytest.approx(2 / 3, abs=0.01)

    def test_failure_rate_counts_pipeline_errors(self) -> None:
        results = [
            _make_eval_result("a", 0.0, pipeline_error="timeout"),
            _make_eval_result("b", 4.0),
        ]
        stats = summary_stats(results)
        assert stats["failure_rate"] == pytest.approx(0.5)

    def test_generated_at_present(self) -> None:
        stats = summary_stats([_make_eval_result()])
        assert "generated_at" in stats
        assert isinstance(stats["generated_at"], str)
        assert len(stats["generated_at"]) > 10

    def test_all_pass(self) -> None:
        results = [_make_eval_result("a", 4.0), _make_eval_result("b", 5.0)]
        assert summary_stats(results)["pass_rate"] == pytest.approx(1.0)

    def test_all_fail(self) -> None:
        results = [_make_eval_result("a", 0.0), _make_eval_result("b", 1.0)]
        assert summary_stats(results)["pass_rate"] == pytest.approx(0.0)


# ── run_scenario_eval ─────────────────────────────────────────────────────────


def _fake_settings(tmp_path: Path) -> Any:
    """Real Settings instance with test values — for run_scenario_eval tests."""
    from sentinel.config import Settings  # noqa: PLC0415

    return Settings(
        groq_api_key="gsk_test",
        sentinel_triage_model="groq/llama-3.1-8b-instant",
        sentinel_analysis_model="groq/llama-3.3-70b-versatile",
        sentinel_judge_model="groq/llama-3.1-8b-instant",
        sentinel_db_path=tmp_path / "test.db",
    )


class TestRunScenarioEval:
    async def test_returns_eval_run_result(self, tmp_path: Path) -> None:
        scenario = _make_scenario_mock()
        stm = _make_stm_mock()
        traj_score = _make_trajectory_score(3.5)

        with (
            patch("sentinel.eval.runner.generate_alert", return_value=_make_alert_mock()),
            patch("sentinel.eval.runner.build_triage_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_log_analyst_agent", return_value=MagicMock()),
            patch(
                "sentinel.eval.runner.build_deploy_correlator_agent",
                return_value=MagicMock(),
            ),
            patch("sentinel.eval.runner.build_remediation_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_comms_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_orchestrator_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.AsyncOpenAI", return_value=_make_groq_client()),
            patch("sentinel.eval.runner.Runner.run", new_callable=AsyncMock),
            patch("sentinel.eval.runner.agent_trace") as mock_trace,
            patch(
                "sentinel.eval.runner.evaluate_trajectory",
                new_callable=AsyncMock,
                return_value=traj_score,
            ),
        ):
            mock_trace.return_value.__enter__ = MagicMock(return_value=None)
            mock_trace.return_value.__exit__ = MagicMock(return_value=False)

            result = await run_scenario_eval(
                scenario,
                short_term_memory=stm,
                trajectories_dir=tmp_path,
                settings=_fake_settings(tmp_path),
                judge_model="test-judge",
                semantic_memory=MagicMock(),
                episodic_memory=MagicMock(),
            )

        assert isinstance(result, EvalRunResult)
        assert result.scenario_id == "bad_deploy_01"

    async def test_pipeline_error_captured(self, tmp_path: Path) -> None:
        scenario = _make_scenario_mock()
        stm = _make_stm_mock()
        traj_score = _make_trajectory_score(0.0)

        with (
            patch("sentinel.eval.runner.generate_alert", return_value=_make_alert_mock()),
            patch("sentinel.eval.runner.build_triage_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_log_analyst_agent", return_value=MagicMock()),
            patch(
                "sentinel.eval.runner.build_deploy_correlator_agent",
                return_value=MagicMock(),
            ),
            patch("sentinel.eval.runner.build_remediation_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_comms_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_orchestrator_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.AsyncOpenAI", return_value=_make_groq_client()),
            patch(
                "sentinel.eval.runner.Runner.run",
                new_callable=AsyncMock,
                side_effect=RuntimeError("LLM timeout"),
            ),
            patch("sentinel.eval.runner.agent_trace") as mock_trace,
            patch(
                "sentinel.eval.runner.evaluate_trajectory",
                new_callable=AsyncMock,
                return_value=traj_score,
            ),
        ):
            mock_trace.return_value.__enter__ = MagicMock(return_value=None)
            mock_trace.return_value.__exit__ = MagicMock(return_value=False)

            result = await run_scenario_eval(
                scenario,
                short_term_memory=stm,
                trajectories_dir=tmp_path,
                settings=_fake_settings(tmp_path),
                judge_model="test-judge",
                semantic_memory=MagicMock(),
                episodic_memory=MagicMock(),
            )

        assert result.pipeline_error == "LLM timeout"

    async def test_trajectory_loaded_from_disk(self, tmp_path: Path) -> None:
        scenario = _make_scenario_mock()
        stm = _make_stm_mock()
        traj_score = _make_trajectory_score(4.0)

        # Pre-write a fake trajectory file with the expected incident_id pattern
        alert_mock = _make_alert_mock()
        expected_incident_id = f"eval-bad_deploy_01-{alert_mock.alert_id[:8]}"
        traj_data: dict[str, Any] = {
            "incident_id": expected_incident_id,
            "spans": [],
        }
        traj_file = tmp_path / f"{expected_incident_id}.json"
        traj_file.write_text(json.dumps(traj_data), encoding="utf-8")

        captured_trajectory: dict[str, Any] = {}

        async def capture_traj(traj: dict[str, Any], *args: Any, **kwargs: Any) -> TrajectoryScore:
            captured_trajectory.update(traj)
            return traj_score

        with (
            patch("sentinel.eval.runner.generate_alert", return_value=alert_mock),
            patch("sentinel.eval.runner.build_triage_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_log_analyst_agent", return_value=MagicMock()),
            patch(
                "sentinel.eval.runner.build_deploy_correlator_agent",
                return_value=MagicMock(),
            ),
            patch("sentinel.eval.runner.build_remediation_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_comms_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_orchestrator_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.AsyncOpenAI", return_value=_make_groq_client()),
            patch("sentinel.eval.runner.Runner.run", new_callable=AsyncMock),
            patch("sentinel.eval.runner.agent_trace") as mock_trace,
            patch(
                "sentinel.eval.runner.evaluate_trajectory",
                new_callable=AsyncMock,
                side_effect=capture_traj,
            ),
        ):
            mock_trace.return_value.__enter__ = MagicMock(return_value=None)
            mock_trace.return_value.__exit__ = MagicMock(return_value=False)

            await run_scenario_eval(
                scenario,
                short_term_memory=stm,
                trajectories_dir=tmp_path,
                settings=_fake_settings(tmp_path),
                judge_model="test-judge",
                semantic_memory=MagicMock(),
                episodic_memory=MagicMock(),
            )

        assert captured_trajectory.get("incident_id") == expected_incident_id

    async def test_trajectory_missing_flagged(self, tmp_path: Path) -> None:
        scenario = _make_scenario_mock()
        stm = _make_stm_mock()
        traj_score = _make_trajectory_score(2.0)

        with (
            patch("sentinel.eval.runner.generate_alert", return_value=_make_alert_mock()),
            patch("sentinel.eval.runner.build_triage_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_log_analyst_agent", return_value=MagicMock()),
            patch(
                "sentinel.eval.runner.build_deploy_correlator_agent",
                return_value=MagicMock(),
            ),
            patch("sentinel.eval.runner.build_remediation_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_comms_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.build_orchestrator_agent", return_value=MagicMock()),
            patch("sentinel.eval.runner.AsyncOpenAI", return_value=_make_groq_client()),
            patch("sentinel.eval.runner.Runner.run", new_callable=AsyncMock),
            patch("sentinel.eval.runner.agent_trace") as mock_trace,
            patch(
                "sentinel.eval.runner.evaluate_trajectory",
                new_callable=AsyncMock,
                return_value=traj_score,
            ),
        ):
            mock_trace.return_value.__enter__ = MagicMock(return_value=None)
            mock_trace.return_value.__exit__ = MagicMock(return_value=False)

            result = await run_scenario_eval(
                scenario,
                short_term_memory=stm,
                trajectories_dir=tmp_path,  # no file written
                settings=_fake_settings(tmp_path),
                judge_model="test-judge",
                semantic_memory=MagicMock(),
                episodic_memory=MagicMock(),
            )

        assert result.trajectory_missing is True


# ── run_eval ──────────────────────────────────────────────────────────────────


def _settings_mock(tmp_path: Path) -> MagicMock:
    """Return a configured settings mock for run_eval tests."""
    s = MagicMock()
    s.sentinel_db_path = tmp_path / "test.db"
    s.sentinel_embedding_model = "all-MiniLM-L6-v2"
    s.groq_api_key = "test-key"
    s.groq_base_url = "https://api.groq.com/openai/v1"
    s.sentinel_judge_model = "llama-3.1-8b-instant"
    s.sentinel_max_tool_calls = 15
    s.sentinel_analysis_model = "llama-3.3-70b-versatile"
    s.sentinel_triage_model = "llama-3.1-8b-instant"
    s.sentinel_log_level = "INFO"
    return s


# Fake data.seed module to satisfy the lazy import in run_eval.
_FAKE_SEED_MOD = MagicMock()
_FAKE_SEED_MOD.seed = AsyncMock()


class TestRunEval:
    async def test_returns_list_of_results(self, tmp_path: Path) -> None:
        with (
            patch("sentinel.eval.runner.get_settings", return_value=_settings_mock(tmp_path)),
            patch("sentinel.eval.runner.configure_logging"),
            patch("sentinel.eval.runner.create_tables", new_callable=AsyncMock),
            patch("sentinel.eval.runner.EmbeddingClient"),
            patch("sentinel.eval.runner.SemanticMemory"),
            patch("sentinel.eval.runner.EpisodicMemory"),
            patch("sentinel.eval.runner.ShortTermMemory"),
            patch("sentinel.eval.runner.apply_sdk_defaults"),
            patch("sentinel.eval.runner.SentinelTracer"),
            patch("sentinel.eval.runner.add_trace_processor"),
            patch("sentinel.eval.runner.list_scenarios", return_value=["bad_deploy_01"]),
            patch("sentinel.eval.runner.load_scenario", return_value=_make_scenario_mock()),
            patch(
                "sentinel.eval.runner.run_scenario_eval",
                new_callable=AsyncMock,
                return_value=_make_eval_result("bad_deploy_01", 3.5),
            ),
            patch.dict(sys.modules, {"data": MagicMock(), "data.seed": _FAKE_SEED_MOD}),
        ):
            results = await run_eval(
                scenario_ids=["bad_deploy_01"],
                db_path=tmp_path / "test.db",
                trajectories_dir=tmp_path / "traj",
            )

        assert len(results) == 1
        assert results[0].scenario_id == "bad_deploy_01"

    async def test_unknown_scenario_skipped(self, tmp_path: Path) -> None:
        """Scenarios that don't exist are skipped, not crash the batch."""
        with (
            patch("sentinel.eval.runner.get_settings", return_value=_settings_mock(tmp_path)),
            patch("sentinel.eval.runner.configure_logging"),
            patch("sentinel.eval.runner.create_tables", new_callable=AsyncMock),
            patch("sentinel.eval.runner.EmbeddingClient"),
            patch("sentinel.eval.runner.SemanticMemory"),
            patch("sentinel.eval.runner.EpisodicMemory"),
            patch("sentinel.eval.runner.ShortTermMemory"),
            patch("sentinel.eval.runner.apply_sdk_defaults"),
            patch("sentinel.eval.runner.SentinelTracer"),
            patch("sentinel.eval.runner.add_trace_processor"),
            patch(
                "sentinel.eval.runner.load_scenario",
                side_effect=FileNotFoundError("not found"),
            ),
            patch.dict(sys.modules, {"data": MagicMock(), "data.seed": _FAKE_SEED_MOD}),
        ):
            results = await run_eval(
                scenario_ids=["nonexistent_scenario"],
                db_path=tmp_path / "test.db",
                trajectories_dir=tmp_path / "traj",
            )

        assert results == []

    async def test_uses_all_scenarios_when_none_specified(self, tmp_path: Path) -> None:
        all_ids = ["bad_deploy_01", "db_pool_01"]

        with (
            patch("sentinel.eval.runner.get_settings", return_value=_settings_mock(tmp_path)),
            patch("sentinel.eval.runner.configure_logging"),
            patch("sentinel.eval.runner.create_tables", new_callable=AsyncMock),
            patch("sentinel.eval.runner.EmbeddingClient"),
            patch("sentinel.eval.runner.SemanticMemory"),
            patch("sentinel.eval.runner.EpisodicMemory"),
            patch("sentinel.eval.runner.ShortTermMemory"),
            patch("sentinel.eval.runner.apply_sdk_defaults"),
            patch("sentinel.eval.runner.SentinelTracer"),
            patch("sentinel.eval.runner.add_trace_processor"),
            patch("sentinel.eval.runner.list_scenarios", return_value=all_ids),
            patch(
                "sentinel.eval.runner.load_scenario",
                side_effect=lambda sid: _make_scenario_mock(sid),
            ),
            patch(
                "sentinel.eval.runner.run_scenario_eval",
                new_callable=AsyncMock,
                side_effect=lambda s, **kw: _make_eval_result(s.scenario_id),
            ),
            patch.dict(sys.modules, {"data": MagicMock(), "data.seed": _FAKE_SEED_MOD}),
        ):
            results = await run_eval(
                db_path=tmp_path / "test.db",
                trajectories_dir=tmp_path / "traj",
            )

        assert len(results) == 2
        scenario_ids_returned = {r.scenario_id for r in results}
        assert scenario_ids_returned == set(all_ids)
