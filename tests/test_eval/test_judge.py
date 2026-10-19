"""Tests for eval/judge.py — LLM-as-judge trajectory scoring."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sentinel.config import Settings
from sentinel.eval.judge import (
    LitellmJudgeClient,
    _parse_judge_response,
    _strip_code_fences,
    _zero_score,
    evaluate_trajectory,
    is_judge_conflict,
    make_judge_client,
)
from sentinel.exceptions import ProviderUnavailableError
from sentinel.models.eval_result import EvalDimension, TrajectoryScore

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_judge_response(scores: dict[str, float] | None = None) -> str:
    """Build a minimal valid judge JSON response."""
    if scores is None:
        scores = {str(d): 4.0 for d in EvalDimension}
    dimension_scores = [
        {"dimension": dim, "score": score, "reasoning": f"OK for {dim}"}
        for dim, score in scores.items()
    ]
    return json.dumps({"dimension_scores": dimension_scores})


def _all_dim_scores(score: float = 4.0) -> dict[str, float]:
    return {str(d): score for d in EvalDimension}


def _make_client(content: str = "") -> MagicMock:
    """Create a mock JudgeClient whose ``complete`` returns a fixed response."""
    client = MagicMock()
    client.complete = AsyncMock(return_value=content or _make_judge_response())
    return client


def _sample_trajectory() -> dict[str, Any]:
    return {
        "incident_id": "inc-001",
        "scenario_id": "bad_deploy_01",
        "timeline": [
            {"agent": "triage_agent", "action": "get_service_metadata", "result": "ok"},
        ],
    }


def _sample_ground_truth() -> dict[str, Any]:
    return {
        "severity": "P1",
        "affected_service": "api-gateway",
        "root_cause_summary": "Missing config key",
        "recommended_action": "rollback",
        "deploy_id": "deploy-abc123",
    }


# ── _strip_code_fences ────────────────────────────────────────────────────────


class TestStripCodeFences:
    def test_strips_json_fence(self) -> None:
        raw = '```json\n{"key": 1}\n```'
        assert _strip_code_fences(raw) == '{"key": 1}'

    def test_strips_plain_fence(self) -> None:
        raw = '```\n{"key": 1}\n```'
        assert _strip_code_fences(raw) == '{"key": 1}'

    def test_no_fence_unchanged(self) -> None:
        raw = '{"key": 1}'
        assert _strip_code_fences(raw) == '{"key": 1}'

    def test_strips_surrounding_whitespace(self) -> None:
        raw = '  \n{"key": 1}\n  '
        assert _strip_code_fences(raw) == '{"key": 1}'


# ── _parse_judge_response ─────────────────────────────────────────────────────


class TestParseJudgeResponse:
    def test_all_five_dimensions_returned(self) -> None:
        raw = _make_judge_response(_all_dim_scores(4.0))
        result = _parse_judge_response(raw, "llama", "inc-001", "bad_deploy_01")
        assert len(result.dimension_scores) == len(EvalDimension) == 5

    def test_scores_match_input(self) -> None:
        raw = _make_judge_response(_all_dim_scores(3.5))
        result = _parse_judge_response(raw, "llama", "inc-001", "bad_deploy_01")
        for ds in result.dimension_scores:
            assert ds.score == pytest.approx(3.5)

    def test_total_score_is_mean(self) -> None:
        scores = {str(d): float(i) for i, d in enumerate(EvalDimension)}
        raw = _make_judge_response(scores)
        result = _parse_judge_response(raw, "llama", "inc-001", "bad_deploy_01")
        expected = sum(scores.values()) / len(scores)
        assert result.total_score == pytest.approx(expected, abs=1e-3)

    def test_judge_model_set(self) -> None:
        raw = _make_judge_response(_all_dim_scores())
        result = _parse_judge_response(raw, "my-model", "inc-001", "s1")
        assert result.judge_model == "my-model"

    def test_incident_and_scenario_ids_set(self) -> None:
        raw = _make_judge_response(_all_dim_scores())
        result = _parse_judge_response(raw, "m", "inc-xyz", "scenario-99")
        assert result.incident_id == "inc-xyz"
        assert result.scenario_id == "scenario-99"

    def test_score_clamped_above_5(self) -> None:
        scores = _all_dim_scores()
        scores["triage_accuracy"] = 99.0
        raw = _make_judge_response(scores)
        result = _parse_judge_response(raw, "m", "i", "s")
        triage = next(
            ds for ds in result.dimension_scores if ds.dimension == EvalDimension.TRIAGE_ACCURACY
        )
        assert triage.score == pytest.approx(5.0)

    def test_score_clamped_below_0(self) -> None:
        scores = _all_dim_scores()
        scores["triage_accuracy"] = -3.0
        raw = _make_judge_response(scores)
        result = _parse_judge_response(raw, "m", "i", "s")
        triage = next(
            ds for ds in result.dimension_scores if ds.dimension == EvalDimension.TRIAGE_ACCURACY
        )
        assert triage.score == pytest.approx(0.0)

    def test_missing_dimension_gets_zero(self) -> None:
        scores = {str(d): 4.0 for d in EvalDimension if d != EvalDimension.MTTR}
        raw = _make_judge_response(scores)
        result = _parse_judge_response(raw, "m", "i", "s")
        mttr = next(ds for ds in result.dimension_scores if ds.dimension == EvalDimension.MTTR)
        assert mttr.score == pytest.approx(0.0)

    def test_json_in_code_fence_parsed(self) -> None:
        inner = _make_judge_response(_all_dim_scores(3.0))
        raw = f"```json\n{inner}\n```"
        result = _parse_judge_response(raw, "m", "i", "s")
        assert len(result.dimension_scores) == len(EvalDimension) == 5

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _parse_judge_response("not json at all", "m", "i", "s")


# ── _zero_score ───────────────────────────────────────────────────────────────


class TestZeroScore:
    def test_returns_trajectory_score(self) -> None:
        result = _zero_score("inc-1", "s-1", "model-x")
        assert isinstance(result, TrajectoryScore)

    def test_all_scores_zero(self) -> None:
        result = _zero_score("inc-1", "s-1", "m")
        assert all(ds.score == 0.0 for ds in result.dimension_scores)

    def test_total_score_zero(self) -> None:
        result = _zero_score("inc-1", "s-1", "m")
        assert result.total_score == 0.0

    def test_all_five_dimensions_present(self) -> None:
        result = _zero_score("inc-1", "s-1", "m")
        dims = {ds.dimension for ds in result.dimension_scores}
        assert dims == set(EvalDimension)


# ── evaluate_trajectory ───────────────────────────────────────────────────────


class TestEvaluateTrajectory:
    async def test_returns_trajectory_score(self) -> None:
        client = _make_client(_make_judge_response(_all_dim_scores(4.0)))
        result = await evaluate_trajectory(
            _sample_trajectory(),
            _sample_ground_truth(),
            incident_id="inc-001",
            scenario_id="bad_deploy_01",
            client=client,
            model="openai/gpt-4.1",
        )
        assert isinstance(result, TrajectoryScore)

    async def test_correct_ids_in_result(self) -> None:
        client = _make_client(_make_judge_response(_all_dim_scores(3.0)))
        result = await evaluate_trajectory(
            _sample_trajectory(),
            _sample_ground_truth(),
            incident_id="inc-test",
            scenario_id="my_scenario",
            client=client,
            model="openai/gpt-4.1",
        )
        assert result.incident_id == "inc-test"
        assert result.scenario_id == "my_scenario"

    async def test_judge_model_in_result(self) -> None:
        client = _make_client(_make_judge_response(_all_dim_scores(4.0)))
        result = await evaluate_trajectory(
            _sample_trajectory(),
            _sample_ground_truth(),
            incident_id="i",
            scenario_id="s",
            client=client,
            model="test-model",
        )
        assert result.judge_model == "test-model"

    async def test_scores_propagated(self) -> None:
        client = _make_client(_make_judge_response(_all_dim_scores(5.0)))
        result = await evaluate_trajectory(
            _sample_trajectory(),
            _sample_ground_truth(),
            incident_id="i",
            scenario_id="s",
            client=client,
            model="openai/gpt-4.1",
        )
        assert result.total_score == pytest.approx(5.0)

    async def test_api_call_uses_system_prompt(self) -> None:
        client = _make_client()
        await evaluate_trajectory(
            _sample_trajectory(),
            _sample_ground_truth(),
            incident_id="i",
            scenario_id="s",
            client=client,
            model="openai/gpt-4.1",
        )
        call_args = client.complete.call_args
        messages = call_args.kwargs["messages"]
        system_msg = next(m for m in messages if m["role"] == "system")
        assert "triage_accuracy" in system_msg["content"]

    async def test_api_call_includes_trajectory_in_user_message(self) -> None:
        client = _make_client()
        await evaluate_trajectory(
            {"incident_id": "inc-sentinel", "extra": "data"},
            _sample_ground_truth(),
            incident_id="i",
            scenario_id="s",
            client=client,
            model="openai/gpt-4.1",
        )
        call_args = client.complete.call_args
        messages = call_args.kwargs["messages"]
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "inc-sentinel" in user_msg["content"]

    async def test_retries_on_json_error(self) -> None:
        """First attempt returns bad JSON, second returns valid response."""
        good_content = _make_judge_response(_all_dim_scores(2.0))
        client = MagicMock()
        client.complete = AsyncMock(side_effect=["NOT VALID JSON", good_content])

        with patch("sentinel.eval.judge.asyncio.sleep", new_callable=AsyncMock):
            result = await evaluate_trajectory(
                _sample_trajectory(),
                _sample_ground_truth(),
                incident_id="i",
                scenario_id="s",
                client=client,
                model="openai/gpt-4.1",
            )

        assert result.total_score == pytest.approx(2.0)
        assert client.complete.call_count == 2

    async def test_all_retries_fail_returns_zero_score(self) -> None:
        """All 3 attempts fail — should return a zero-scored result."""
        client = MagicMock()
        client.complete = AsyncMock(return_value="INVALID")

        with patch("sentinel.eval.judge.asyncio.sleep", new_callable=AsyncMock):
            result = await evaluate_trajectory(
                _sample_trajectory(),
                _sample_ground_truth(),
                incident_id="i",
                scenario_id="s",
                client=client,
                model="openai/gpt-4.1",
            )

        assert result.total_score == pytest.approx(0.0)
        assert result.incident_id == "i"

    async def test_api_exception_retries_then_returns_zero(self) -> None:
        client = MagicMock()
        client.complete = AsyncMock(side_effect=RuntimeError("Network error"))

        with patch("sentinel.eval.judge.asyncio.sleep", new_callable=AsyncMock):
            result = await evaluate_trajectory(
                _sample_trajectory(),
                _sample_ground_truth(),
                incident_id="i",
                scenario_id="s",
                client=client,
                model="openai/gpt-4.1",
            )

        assert result.total_score == 0.0

    async def test_temperature_zero_in_api_call(self) -> None:
        client = _make_client()
        await evaluate_trajectory(
            _sample_trajectory(),
            _sample_ground_truth(),
            incident_id="i",
            scenario_id="s",
            client=client,
            model="openai/gpt-4.1",
        )
        call_kwargs = client.complete.call_args.kwargs
        assert call_kwargs.get("temperature") == 0.0


# ── make_judge_client: the non-primary provider (§4.7, decision 2026-10-15) ───


def _keys(**kwargs: Any) -> Settings:
    base: dict[str, Any] = {"anthropic_api_key": "sk-ant-test", "openai_api_key": "sk-test"}
    base.update(kwargs)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class TestMakeJudgeClient:
    def test_primary_anthropic_judges_on_openai(self) -> None:
        model, client = make_judge_client(_keys())
        assert model == "openai/gpt-4.1"
        assert isinstance(client, LitellmJudgeClient)

    def test_primary_openai_judges_on_anthropic(self) -> None:
        model, _ = make_judge_client(_keys(sentinel_primary_provider="openai"))
        assert model == "anthropic/claude-opus-4-1"

    def test_missing_judge_key_raises_without_falling_back(self) -> None:
        """No fallback for the judge: it would land on the agents' provider."""
        with pytest.raises(ProviderUnavailableError, match="OPENAI_API_KEY"):
            make_judge_client(_keys(openai_api_key=None))

    async def test_client_calls_litellm_with_the_judge_key(self) -> None:
        _, client = make_judge_client(_keys())
        response = MagicMock()
        response.choices[0].message.content = "{}"
        with patch(
            "sentinel.eval.judge.litellm.acompletion", new_callable=AsyncMock
        ) as acompletion:
            acompletion.return_value = response
            text = await client.complete(
                model="openai/gpt-4.1",
                messages=[{"role": "user", "content": "x"}],
                temperature=0.0,
                max_tokens=10,
            )
        assert text == "{}"
        kwargs = acompletion.call_args.kwargs
        assert kwargs["model"] == "openai/gpt-4.1"
        assert kwargs["api_key"] == "sk-test"


# ── Runtime judge_conflict guard (decision 2026-10-18) ────────────────────────


class TestJudgeConflictGuard:
    def test_is_judge_conflict(self) -> None:
        assert is_judge_conflict("openai/gpt-4.1", ["anthropic/claude-haiku-4-5", "openai/gpt-4.1"])
        assert not is_judge_conflict("openai/gpt-4.1", ["openai/gpt-4o", "openai/gpt-4o-mini"])
        assert not is_judge_conflict("openai/gpt-4.1", [])

    async def test_a_run_that_used_the_judge_model_is_not_judged(self) -> None:
        client = _make_client()
        trajectory = {
            **_sample_trajectory(),
            "pipeline_result": {"models_used": ["anthropic/claude-haiku-4-5", "openai/gpt-4.1"]},
        }
        with patch("sentinel.eval.judge.logger") as log:
            result = await evaluate_trajectory(
                trajectory,
                _sample_ground_truth(),
                incident_id="inc-001",
                scenario_id="bad_deploy_01",
                client=client,
                model="openai/gpt-4.1",
            )
        client.complete.assert_not_awaited()
        assert result.judge_conflict is True
        assert result.total_score == 0.0
        assert all(ds.score == 0.0 for ds in result.dimension_scores)
        log.error.assert_called_once()
        assert log.error.call_args.args[0] == "judge_conflict"

    async def test_a_run_on_other_models_is_judged(self) -> None:
        client = _make_client()
        trajectory = {
            **_sample_trajectory(),
            "pipeline_result": {"models_used": ["openai/gpt-4o-mini", "openai/gpt-4o"]},
        }
        result = await evaluate_trajectory(
            trajectory,
            _sample_ground_truth(),
            incident_id="inc-001",
            scenario_id="bad_deploy_01",
            client=client,
            model="anthropic/claude-opus-4-1",
        )
        client.complete.assert_awaited_once()
        assert result.judge_conflict is False
        assert result.total_score == pytest.approx(4.0)
