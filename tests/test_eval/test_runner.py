"""Tests for eval/runner.py — batch eval runner."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents.tracing import get_trace_provider

from sentinel.agents.orchestrator import PIPELINE_ERROR
from sentinel.eval.runner import (
    EvalRunResult,
    run_eval,
    run_scenario_eval,
    scores_from_results,
    summary_stats,
)
from sentinel.infra.tracing import SentinelTracer, install_trace_processors
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.eval_result import DimensionScore, EvalDimension, TrajectoryScore
from sentinel.models.incident import IncidentStatus
from sentinel.models.pipeline import PipelineResult
from sentinel.models.tools import EscalationContext

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_trajectory_score(total: float = 0.8, *, judge_conflict: bool = False) -> TrajectoryScore:
    return TrajectoryScore(
        incident_id="inc-001",
        scenario_id="bad_deploy_01",
        dimension_scores=[
            DimensionScore(dimension=d, score=total, reasoning="ok") for d in EvalDimension
        ],
        total_score=total,
        judge_model="test-model",
        judge_conflict=judge_conflict,
    )


def _make_eval_result(
    scenario_id: str = "bad_deploy_01",
    total: float = 0.8,
    pipeline_error: str | None = None,
    trajectory_missing: bool = False,
    judge_conflict: bool = False,
) -> EvalRunResult:
    return EvalRunResult(
        scenario_id=scenario_id,
        incident_id=f"eval-{scenario_id}-abc12345",
        score=_make_trajectory_score(total, judge_conflict=judge_conflict),
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


def _judge() -> tuple[str, MagicMock]:
    """What make_judge_client returns: the judge model and a mock JudgeClient."""
    client = MagicMock()
    client.complete = AsyncMock()
    return "openai/gpt-4.1", client


# ── scores_from_results ───────────────────────────────────────────────────────


class TestScoresFromResults:
    def test_extracts_scores(self) -> None:
        results = [_make_eval_result("s1", 0.6), _make_eval_result("s2", 0.8)]
        scores = scores_from_results(results)
        assert len(scores) == 2
        assert all(isinstance(s, TrajectoryScore) for s in scores)

    def test_empty_list(self) -> None:
        assert scores_from_results([]) == []

    def test_order_preserved(self) -> None:
        results = [_make_eval_result("a", 0.2), _make_eval_result("b", 1.0)]
        scores = scores_from_results(results)
        assert scores[0].total_score == pytest.approx(0.2)
        assert scores[1].total_score == pytest.approx(1.0)


# ── summary_stats ─────────────────────────────────────────────────────────────


class TestSummaryStats:
    def test_empty_returns_zeros(self) -> None:
        stats = summary_stats([])
        assert stats["avg_total"] == pytest.approx(0.0)
        assert stats["pass_rate"] == pytest.approx(0.0)

    def test_avg_total_is_mean(self) -> None:
        results = [_make_eval_result("a", 0.4), _make_eval_result("b", 0.8)]
        stats = summary_stats(results)
        assert stats["avg_total"] == pytest.approx(0.6)

    def test_pass_rate_with_threshold_3(self) -> None:
        # 0.4 fails, 0.6 passes, 0.8 passes → 2/3 pass
        results = [
            _make_eval_result("a", 0.4),
            _make_eval_result("b", 0.6),
            _make_eval_result("c", 0.8),
        ]
        stats = summary_stats(results)
        assert stats["pass_rate"] == pytest.approx(2 / 3, abs=0.01)

    def test_failure_rate_counts_pipeline_errors(self) -> None:
        results = [
            _make_eval_result("a", 0.0, pipeline_error="timeout"),
            _make_eval_result("b", 0.8),
        ]
        stats = summary_stats(results)
        assert stats["failure_rate"] == pytest.approx(0.5)

    def test_generated_at_present(self) -> None:
        stats = summary_stats([_make_eval_result()])
        assert "generated_at" in stats
        assert isinstance(stats["generated_at"], str)
        assert len(stats["generated_at"]) > 10

    def test_all_pass(self) -> None:
        results = [_make_eval_result("a", 0.8), _make_eval_result("b", 1.0)]
        assert summary_stats(results)["pass_rate"] == pytest.approx(1.0)

    def test_all_fail(self) -> None:
        results = [_make_eval_result("a", 0.0), _make_eval_result("b", 0.2)]
        assert summary_stats(results)["pass_rate"] == pytest.approx(0.0)

    def test_judge_conflicts_are_counted_and_excluded(self) -> None:
        """Decision 2026-10-18: a judge_conflict score is invalid — not in any figure."""
        results = [
            _make_eval_result("a", 0.8),
            _make_eval_result("b", 0.0, pipeline_error="x", judge_conflict=True),
        ]
        stats = summary_stats(results)
        assert stats["avg_total"] == pytest.approx(0.8)
        assert stats["pass_rate"] == pytest.approx(1.0)
        assert stats["failure_rate"] == pytest.approx(0.0)
        assert stats["judge_conflicts"] == 1

    def test_only_conflicts_gives_zeros(self) -> None:
        stats = summary_stats([_make_eval_result("a", 1.0, judge_conflict=True)])
        assert stats["avg_total"] == pytest.approx(0.0)
        assert stats["judge_conflicts"] == 1


# ── run_scenario_eval ─────────────────────────────────────────────────────────


def _fake_settings(tmp_path: Path) -> Any:
    """Real Settings instance with test values — for run_scenario_eval tests."""
    from sentinel.config import Settings  # noqa: PLC0415

    return Settings(
        anthropic_api_key="sk-ant-test",
        openai_api_key="sk-test",
        sentinel_db_path=tmp_path / "test.db",
    )


_INCIDENT = uuid.UUID("0b7e6f3a-4c2d-4e8f-9a1b-5d6c7e8f9a0b")


def _alert() -> AlertPayload:
    return AlertPayload(
        alert_id="alert-abcdef12",
        source=AlertSource.DATADOG,
        service="api-gateway",
        metric="error_rate",
        threshold=0.05,
        current_value=0.34,
        severity=AlertSeverity.CRITICAL,
    )


def _escalated(reason: str = "decision gate: confidence 0.40 is below 0.7") -> PipelineResult:
    return PipelineResult(
        incident_id=str(_INCIDENT),
        status="escalated",
        resolution_type="escalated",
        root_cause="unclear",
        confidence=0.4,
        escalation=EscalationContext(
            hypothesis="unclear", evidence=[], confidence=0.4, missing=[reason]
        ),
        reflexion_loops=2,
        tool_calls_used=3,
        tokens_used=900,
    )


async def _run(
    tmp_path: Path,
    *,
    result: PipelineResult | None = None,
    score: TrajectoryScore | None = None,
    capture: dict[str, Any] | None = None,
    stm: Any = None,
) -> EvalRunResult:
    async def judge(traj: dict[str, Any], *args: Any, **kwargs: Any) -> TrajectoryScore:
        if capture is not None:
            capture.update(traj)
        return score or _make_trajectory_score(0.7)

    with (
        patch("sentinel.eval.runner.generate_alert", return_value=_alert()),
        patch("sentinel.eval.runner.uuid.uuid4", return_value=_INCIDENT),
        patch("sentinel.eval.runner.make_judge_client", return_value=_judge()),
        patch(
            "sentinel.eval.runner.run_pipeline",
            new_callable=AsyncMock,
            return_value=result or _escalated(),
        ),
        patch("sentinel.eval.runner.evaluate_trajectory", side_effect=judge),
    ):
        return await run_scenario_eval(
            _make_scenario_mock(),
            short_term_memory=stm if stm is not None else _make_stm_mock(),
            trajectories_dir=tmp_path,
            settings=_fake_settings(tmp_path),
            semantic_memory=MagicMock(),
            episodic_memory=MagicMock(),
        )


class TestRunScenarioEval:
    @pytest.mark.parametrize("ran_judge_model", [True, False])
    async def test_the_judge_conflict_guard_runs_on_models_used(
        self, tmp_path: Path, ran_judge_model: bool
    ) -> None:
        """The real evaluate_trajectory: a run whose models_used holds the judge's model is
        never sent to the judge and comes back marked judge_conflict."""
        judge_model, client = _judge()
        client.complete = AsyncMock(
            return_value=json.dumps(
                {
                    "dimension_scores": [
                        {"dimension": str(d), "score": 0.8, "reasoning": "ok"}
                        for d in EvalDimension
                    ]
                }
            )
        )
        result = _escalated()
        result.models_used = ["anthropic/claude-haiku-4-5"] + (
            [judge_model] if ran_judge_model else []
        )
        with (
            patch("sentinel.eval.runner.generate_alert", return_value=_alert()),
            patch("sentinel.eval.runner.uuid.uuid4", return_value=_INCIDENT),
            patch("sentinel.eval.runner.make_judge_client", return_value=(judge_model, client)),
            patch("sentinel.eval.runner.run_pipeline", new_callable=AsyncMock, return_value=result),
        ):
            run = await run_scenario_eval(
                _make_scenario_mock(),
                short_term_memory=_make_stm_mock(),
                trajectories_dir=tmp_path,
                settings=_fake_settings(tmp_path),
                semantic_memory=MagicMock(),
                episodic_memory=MagicMock(),
            )
        assert run.score.judge_conflict is ran_judge_model
        if ran_judge_model:
            client.complete.assert_not_awaited()
            assert run.score.total_score == 0.0
        else:
            client.complete.assert_awaited_once()
            assert run.score.total_score == pytest.approx(0.8)

    async def test_returns_eval_run_result(self, tmp_path: Path) -> None:
        result = await _run(tmp_path)
        assert isinstance(result, EvalRunResult)
        assert result.scenario_id == "bad_deploy_01"
        assert result.incident_id == str(_INCIDENT)  # a UUID, like every incident id
        assert result.pipeline_error is None

    async def test_pipeline_error_captured(self, tmp_path: Path) -> None:
        reason = f"{PIPELINE_ERROR} during analysis: TimeoutError"
        result = await _run(tmp_path, result=_escalated(reason))
        assert result.pipeline_error == reason

    async def test_trajectory_loaded_from_disk(self, tmp_path: Path) -> None:
        traj_data: dict[str, Any] = {"incident_id": str(_INCIDENT), "spans": []}
        (tmp_path / f"{_INCIDENT}.json").write_text(json.dumps(traj_data), encoding="utf-8")
        captured: dict[str, Any] = {}
        await _run(tmp_path, capture=captured)
        assert captured.get("incident_id") == str(_INCIDENT)
        # The judge also gets the outcome: status, loops, budget, tokens.
        assert captured["pipeline_result"]["status"] == "escalated"
        assert captured["pipeline_result"]["reflexion_loops"] == 2

    async def test_trajectory_missing_flagged(self, tmp_path: Path) -> None:
        result = await _run(tmp_path)  # no file written
        assert result.trajectory_missing is True

    async def test_the_result_status_reaches_short_term_memory(self, tmp_path: Path) -> None:
        stm = ShortTermMemory()
        await _run(tmp_path, stm=stm)
        context = stm.get_context(str(_INCIDENT))
        assert context["status"] == IncidentStatus.ESCALATED
        assert context["result"]["resolution_type"] == "escalated"

    async def test_the_pipeline_runs_for_real_in_fake_mode(self, tmp_path: Path) -> None:
        """SENTINEL_FAKE_LLM=1: the real orchestrator and agents run; only the judge is
        stubbed (it is phase 6's, and needs the other provider's key)."""
        from sentinel.config import Settings  # noqa: PLC0415

        stm = ShortTermMemory()
        tracer = SentinelTracer(stm, trajectories_dir=tmp_path)
        multi: Any = get_trace_provider()._multi_processor  # type: ignore[attr-defined]
        before = list(multi._processors)
        captured: dict[str, Any] = {}

        async def judge(traj: dict[str, Any], *args: Any, **kwargs: Any) -> TrajectoryScore:
            captured.update(traj)
            return _make_trajectory_score(0.2)

        try:
            install_trace_processors(tracer)
            with (
                patch("sentinel.eval.runner.generate_alert", return_value=_alert()),
                patch("sentinel.eval.runner.make_judge_client", return_value=_judge()),
                patch("sentinel.eval.runner.evaluate_trajectory", side_effect=judge),
            ):
                result = await run_scenario_eval(
                    _make_scenario_mock(),
                    short_term_memory=stm,
                    trajectories_dir=tmp_path,
                    settings=Settings(_env_file=None, sentinel_fake_llm=True),  # type: ignore[call-arg]
                    semantic_memory=SemanticMemory(None),
                    episodic_memory=EpisodicMemory(None, MagicMock()),
                )
        finally:
            multi.set_processors(before)

        assert result.pipeline_error is None
        assert result.trajectory_missing is False  # SentinelTracer wrote the trace file
        assert captured["pipeline_result"]["status"] == "escalated"
        assert stm.get_context(result.incident_id)["status"] == IncidentStatus.ESCALATED


# ── run_eval ──────────────────────────────────────────────────────────────────


def _settings_mock(tmp_path: Path) -> MagicMock:
    """Return a configured settings mock for run_eval tests."""
    s = MagicMock()
    s.sentinel_db_path = tmp_path / "test.db"
    s.sentinel_embedding_model = "all-MiniLM-L6-v2"
    s.sentinel_primary_provider = "anthropic"
    s.sentinel_max_tool_calls = 20
    s.sentinel_log_level = "INFO"
    s.database_url = ""  # no Postgres: run_eval builds episodic memory with pool=None
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
            patch("sentinel.eval.runner.install_trace_processors"),
            patch("sentinel.eval.runner.list_scenarios", return_value=["bad_deploy_01"]),
            patch("sentinel.eval.runner.load_scenario", return_value=_make_scenario_mock()),
            patch(
                "sentinel.eval.runner.run_scenario_eval",
                new_callable=AsyncMock,
                return_value=_make_eval_result("bad_deploy_01", 0.7),
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

    async def test_memory_gets_the_pool_and_pool_is_closed(self, tmp_path: Path) -> None:
        """run_eval opens the Postgres pool, hands it to both memories, closes it after."""
        pool = MagicMock(name="pool")
        with (
            patch("sentinel.eval.runner.get_settings", return_value=_settings_mock(tmp_path)),
            patch("sentinel.eval.runner.configure_logging"),
            patch("sentinel.eval.runner.create_tables", new_callable=AsyncMock),
            patch(
                "sentinel.eval.runner.init_pool_from_settings",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch("sentinel.eval.runner.close_pool", new_callable=AsyncMock) as close_pool,
            patch("sentinel.eval.runner.EmbeddingClient") as embedding_cls,
            patch("sentinel.eval.runner.SemanticMemory") as semantic_cls,
            patch("sentinel.eval.runner.EpisodicMemory") as episodic_cls,
            patch("sentinel.eval.runner.ShortTermMemory"),
            patch("sentinel.eval.runner.apply_sdk_defaults"),
            patch("sentinel.eval.runner.SentinelTracer"),
            patch("sentinel.eval.runner.install_trace_processors"),
            patch("sentinel.eval.runner.load_scenario", side_effect=FileNotFoundError("x")),
            patch.dict(sys.modules, {"data": MagicMock(), "data.seed": _FAKE_SEED_MOD}),
        ):
            await run_eval(scenario_ids=["missing"], db_path=tmp_path / "test.db")

        semantic_cls.assert_called_once_with(pool)
        episodic_cls.assert_called_once_with(pool, embedding_cls.from_model_name.return_value)
        close_pool.assert_awaited_once()

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
            patch("sentinel.eval.runner.install_trace_processors"),
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
            patch("sentinel.eval.runner.install_trace_processors"),
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
