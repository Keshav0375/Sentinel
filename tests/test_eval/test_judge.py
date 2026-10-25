"""Tests for eval/judge.py — LLM-as-judge trajectory scoring."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from sentinel.config import Settings
from sentinel.eval.judge import (
    FAKE_JUDGE_DEFAULT_SCORE,
    FakeJudgeClient,
    LitellmJudgeClient,
    _parse_judge_response,
    _strip_code_fences,
    _zero_score,
    evaluate_trajectory,
    is_judge_conflict,
    make_judge_client,
    push_scores,
    score_incident,
)
from sentinel.exceptions import EvalError, ProviderUnavailableError, SentinelMemoryError
from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.eval_result import DimensionScore, EvalDimension, TrajectoryScore
from sentinel.models.pipeline import PipelineResult
from sentinel.models.tools import EscalationContext, RollbackSpec, TargetDeploy
from sentinel.providers.router import (
    JUDGE_MODELS,
    PROVIDERS,
    agent_models,
    judge_model_string,
    provider_of,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_judge_response(scores: dict[str, float] | None = None) -> str:
    """Build a minimal valid judge JSON response."""
    if scores is None:
        scores = {str(d): 0.8 for d in EvalDimension}
    dimension_scores = [
        {"dimension": dim, "score": score, "reasoning": f"OK for {dim}"}
        for dim, score in scores.items()
    ]
    return json.dumps({"dimension_scores": dimension_scores})


def _all_dim_scores(score: float = 0.8) -> dict[str, float]:
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
        "branch": "deployfail/01",
        "case": "ii",
        "fault": "requirements.txt pins a version that does not exist",
        "expected_signal_type": "deploy_failure",
        "expected_resolution": "rollback",
        "expected_failed_stage": "build",
        "also_expected": [],
        "expected_culprit": "pr-41-abc1234",
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
        raw = _make_judge_response(_all_dim_scores(0.8))
        result = _parse_judge_response(raw, "llama", "inc-001", "bad_deploy_01")
        assert len(result.dimension_scores) == len(EvalDimension) == 5

    def test_scores_match_input(self) -> None:
        raw = _make_judge_response(_all_dim_scores(0.7))
        result = _parse_judge_response(raw, "llama", "inc-001", "bad_deploy_01")
        for ds in result.dimension_scores:
            assert ds.score == pytest.approx(0.7)

    def test_total_score_is_mean(self) -> None:
        scores = {str(d): i / 4 for i, d in enumerate(EvalDimension)}
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

    def test_score_clamped_above_1(self) -> None:
        scores = _all_dim_scores()
        scores["triage_accuracy"] = 99.0
        raw = _make_judge_response(scores)
        result = _parse_judge_response(raw, "m", "i", "s")
        triage = next(
            ds for ds in result.dimension_scores if ds.dimension == EvalDimension.TRIAGE_ACCURACY
        )
        assert triage.score == pytest.approx(1.0)

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
        scores = {str(d): 0.8 for d in EvalDimension if d != EvalDimension.MTTR}
        raw = _make_judge_response(scores)
        result = _parse_judge_response(raw, "m", "i", "s")
        mttr = next(ds for ds in result.dimension_scores if ds.dimension == EvalDimension.MTTR)
        assert mttr.score == pytest.approx(0.0)

    def test_json_in_code_fence_parsed(self) -> None:
        inner = _make_judge_response(_all_dim_scores(0.6))
        raw = f"```json\n{inner}\n```"
        result = _parse_judge_response(raw, "m", "i", "s")
        assert len(result.dimension_scores) == len(EvalDimension) == 5

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _parse_judge_response("not json at all", "m", "i", "s")

    def test_non_finite_score_raises(self) -> None:
        scores = _all_dim_scores()
        scores["triage_accuracy"] = float("nan")  # json.dumps emits NaN
        raw = _make_judge_response(scores)
        with pytest.raises(ValueError, match="not finite"):
            _parse_judge_response(raw, "m", "i", "s")

    def test_out_of_range_score_is_logged(self) -> None:
        scores = _all_dim_scores()
        scores["triage_accuracy"] = 4.0
        raw = _make_judge_response(scores)
        with patch("sentinel.eval.judge.logger") as log:
            result = _parse_judge_response(raw, "m", "i", "s")
        assert result.triage_accuracy == pytest.approx(1.0)
        log.warning.assert_called_once()
        assert log.warning.call_args.args[0] == "judge_score_out_of_range"


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

    def test_judge_failed_flag(self) -> None:
        result = _zero_score("i", "s", "m", judge_failed=True)
        assert result.judge_failed is True
        assert result.judge_conflict is False
        assert all(
            ds.reasoning == "Judge failed after retries — score invalid."
            for ds in result.dimension_scores
        )


# ── evaluate_trajectory ───────────────────────────────────────────────────────


class TestEvaluateTrajectory:
    async def test_returns_trajectory_score(self) -> None:
        client = _make_client(_make_judge_response(_all_dim_scores(0.8)))
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
        client = _make_client(_make_judge_response(_all_dim_scores(0.6)))
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
        client = _make_client(_make_judge_response(_all_dim_scores(0.8)))
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
        client = _make_client(_make_judge_response(_all_dim_scores(1.0)))
        result = await evaluate_trajectory(
            _sample_trajectory(),
            _sample_ground_truth(),
            incident_id="i",
            scenario_id="s",
            client=client,
            model="openai/gpt-4.1",
        )
        assert result.total_score == pytest.approx(1.0)

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
        good_content = _make_judge_response(_all_dim_scores(0.4))
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

        assert result.total_score == pytest.approx(0.4)
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
        assert result.judge_failed is True
        assert result.judge_conflict is False

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
        assert result.judge_failed is True

    async def test_success_is_not_judge_failed(self) -> None:
        result = await evaluate_trajectory(
            _sample_trajectory(),
            _sample_ground_truth(),
            incident_id="i",
            scenario_id="s",
            client=_make_client(),
            model="openai/gpt-4.1",
        )
        assert result.judge_failed is False

    def test_evaluate_trajectory_docstring_has_no_stale_keys(self) -> None:
        doc = (evaluate_trajectory.__doc__ or "").lower()
        # Phase-1 ground-truth keys, spelled in parts so the stale-key grep stays clean.
        for word in ("sever" + "ity", "affected" + "_service", "recommended" + "_action"):
            assert word not in doc, word
        for word in ("root_cause" + "_summary", "deploy" + "_id"):
            assert word not in doc, word

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

    @pytest.mark.parametrize("primary", ["anthropic", "openai"])
    def test_fake_llm_needs_no_key(self, primary: str) -> None:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, sentinel_fake_llm=True, sentinel_primary_provider=primary
        )
        model, client = make_judge_client(settings)
        assert model == JUDGE_MODELS[primary]  # type: ignore[index]
        assert isinstance(client, FakeJudgeClient)

    def test_judge_never_grades_its_own_family_or_model(self) -> None:
        for p in PROVIDERS:
            model = judge_model_string(p)
            assert model not in agent_models()
            assert provider_of(model) != p

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
        assert result.total_score == pytest.approx(0.8)


# ── FakeJudgeClient (SENTINEL_FAKE_LLM) ───────────────────────────────────────


class TestFakeJudgeClient:
    async def _score(self, client: FakeJudgeClient) -> TrajectoryScore:
        raw = await client.complete(
            model="openai/gpt-4.1", messages=[], temperature=0.0, max_tokens=10
        )
        return _parse_judge_response(raw, "openai/gpt-4.1", "i", "s")

    async def test_default_scores_every_dimension(self) -> None:
        result = await self._score(FakeJudgeClient())
        assert len(result.dimension_scores) == len(EvalDimension)
        assert all(ds.score == FAKE_JUDGE_DEFAULT_SCORE for ds in result.dimension_scores)

    async def test_scripted_scores_override_the_default(self) -> None:
        result = await self._score(FakeJudgeClient({EvalDimension.TRIAGE_ACCURACY: 0.9}))
        assert result.triage_accuracy == pytest.approx(0.9)
        assert result.mttr == FAKE_JUDGE_DEFAULT_SCORE

    async def test_calls_are_recorded_with_the_model(self) -> None:
        client = FakeJudgeClient()
        await client.complete(
            model="openai/gpt-4.1",
            messages=[{"role": "user", "content": "x"}],
            temperature=0.0,
            max_tokens=10,
        )
        assert client.calls[0]["model"] == "openai/gpt-4.1"


# ── push_scores: LangFuse trace scores (§6.4) ─────────────────────────────────

_TRACE = "4f7c2a5e8f1d4f0e9c4b2b8e6a1d3c70"


def _score_with(
    values: list[float], *, judge_conflict: bool = False, reasoning: str = "ok"
) -> TrajectoryScore:
    return TrajectoryScore(
        incident_id="4f7c2a5e-8f1d-4f0e-9c4b-2b8e6a1d3c70",
        scenario_id="s",
        dimension_scores=[
            DimensionScore(dimension=d, score=v, reasoning=reasoning)
            for d, v in zip(EvalDimension, values, strict=True)
        ],
        total_score=sum(values) / len(values),
        judge_model="openai/gpt-4.1",
        judge_conflict=judge_conflict,
    )


class TestPushScores:
    def test_one_numeric_score_per_dimension(self) -> None:
        values = [0.2, 0.4, 0.6, 0.8, 1.0]
        with patch("sentinel.eval.judge.push_score") as push:
            pushed = push_scores(_score_with(values), _TRACE)
        assert pushed == 5
        assert push.call_args_list == [
            call(_TRACE, str(d), v) for d, v in zip(EvalDimension, values, strict=True)
        ]

    def test_conflict_pushes_nothing(self) -> None:
        with patch("sentinel.eval.judge.push_score") as push:
            pushed = push_scores(_zero_score("i", "s", "m", judge_conflict=True), _TRACE)
        assert pushed == 0
        push.assert_not_called()

    def test_failed_pushes_nothing(self) -> None:
        with (
            patch("sentinel.eval.judge.push_score") as push,
            patch("sentinel.eval.judge.logger") as log,
        ):
            pushed = push_scores(_zero_score("i", "s", "m", judge_failed=True), _TRACE)
        assert pushed == 0
        push.assert_not_called()
        assert log.warning.call_args.args[0] == "judge_failed_scores_not_pushed"

    def test_only_score_values_reach_langfuse(self) -> None:
        """R14: no reasoning or trajectory text leaves the process."""
        score = _score_with([0.5] * 5, reasoning="SECRET-REASONING")
        with patch("sentinel.infra.tracing.get_client") as get_client:
            lf = get_client.return_value
            push_scores(score, _TRACE)
        assert lf.create_score.call_count == 5
        for c in lf.create_score.call_args_list:
            assert set(c.kwargs) == {"trace_id", "name", "value", "data_type"}
            assert c.kwargs["data_type"] == "NUMERIC"
        assert "SECRET-REASONING" not in repr(lf.mock_calls)
        assert {c[0] for c in lf.mock_calls} == {"create_score"}


# ── score_incident: judge → store → push ──────────────────────────────────────

_INC = "4f7c2a5e-8f1d-4f0e-9c4b-2b8e6a1d3c70"


def _result(status: str, models_used: list[str] | None = None) -> PipelineResult:
    common: dict[str, Any] = {
        "incident_id": _INC,
        "root_cause": "pr-42 broke GET /",
        "confidence": 0.9,
        "reflexion_loops": 1,
        "tool_calls_used": 4,
        "tokens_used": 1234,
        "models_used": models_used or ["anthropic/claude-haiku-4-5", "anthropic/claude-sonnet-4-6"],
    }
    if status == "rollback_proposed":
        spec = RollbackSpec(
            incident_id=_INC,
            root_cause="pr-42 broke GET /",
            evidence_summary="checks fail since pr-42",
            confidence=0.9,
            justification="the previous version passed",
            target_deploy=TargetDeploy(
                pr_number=42, commit_sha="a1b2c3d", deployed_at=datetime(2026, 10, 15, tzinfo=UTC)
            ),
        )
        return PipelineResult(
            status="rollback_proposed", resolution_type="rollback", rollback_spec=spec, **common
        )
    escalation = EscalationContext(hypothesis="unclear", evidence=[], confidence=0.3, missing=["x"])
    return PipelineResult(
        status="escalated", resolution_type="escalated", escalation=escalation, **common
    )


def _trajectory(status: str, models_used: list[str] | None = None) -> dict[str, Any]:
    return {
        "incident_id": _INC,
        "pipeline_result": _result(status, models_used).model_dump(mode="json"),
    }


def _episodic(stored: bool = True) -> MagicMock:
    episodic = MagicMock(spec=EpisodicMemory)
    episodic.store_eval_score = AsyncMock(return_value=stored)
    return episodic


class TestScoreIncident:
    @pytest.mark.parametrize("status", ["rollback_proposed", "escalated"])
    async def test_both_paths_are_scored_stored_and_pushed(self, status: str) -> None:
        model, client = make_judge_client(Settings(_env_file=None, sentinel_fake_llm=True))  # type: ignore[call-arg]
        assert isinstance(client, FakeJudgeClient)
        episodic = _episodic()
        with patch("sentinel.eval.judge.push_score") as push:
            score = await score_incident(
                _trajectory(status),
                _sample_ground_truth(),
                incident_id=_INC,
                scenario_id="bad_deploy_01",
                client=client,
                model=model,
                episodic=episodic,
            )
        assert len(client.calls) == 1
        assert not score.judge_conflict
        assert len(score.dimension_scores) == len(EvalDimension)
        assert all(0.0 <= ds.score <= 1.0 and ds.score == 0.5 for ds in score.dimension_scores)
        episodic.store_eval_score.assert_awaited_once_with(_INC, score)
        assert push.call_count == 5
        assert {c.args[0] for c in push.call_args_list} == {uuid.UUID(_INC).hex}

    async def test_a_good_escalation_scores_well(self) -> None:
        client = FakeJudgeClient({d: 0.9 for d in EvalDimension})
        with patch("sentinel.eval.judge.push_score"):
            score = await score_incident(
                _trajectory("escalated"),
                _sample_ground_truth(),
                incident_id=_INC,
                scenario_id="s",
                client=client,
                model="openai/gpt-4.1",
                episodic=_episodic(),
            )
        assert score.total_score == pytest.approx(0.9)

    async def test_judge_conflict_is_stored_not_pushed(self) -> None:
        client = FakeJudgeClient()
        episodic = _episodic()
        trajectory = _trajectory("rollback_proposed", ["openai/gpt-4.1"])
        with patch("sentinel.eval.judge.push_score") as push:
            score = await score_incident(
                trajectory,
                _sample_ground_truth(),
                incident_id=_INC,
                scenario_id="s",
                client=client,
                model="openai/gpt-4.1",
                episodic=episodic,
            )
        assert client.calls == []
        push.assert_not_called()
        stored = episodic.store_eval_score.await_args.args[1]
        assert stored.judge_conflict is True
        assert stored.to_eval_score()["judge_conflict"] is True
        assert score.judge_conflict is True

    async def test_judge_failure_is_stored_flagged_not_pushed(self) -> None:
        client = MagicMock()
        client.complete = AsyncMock(side_effect=RuntimeError("down"))
        episodic = _episodic()
        with (
            patch("sentinel.eval.judge.asyncio.sleep", new_callable=AsyncMock),
            patch("sentinel.eval.judge.push_score") as push,
        ):
            await score_incident(
                _trajectory("escalated"),
                _sample_ground_truth(),
                incident_id=_INC,
                scenario_id="s",
                client=client,
                model="openai/gpt-4.1",
                episodic=episodic,
            )
        push.assert_not_called()
        stored = episodic.store_eval_score.await_args.args[1]
        assert stored.judge_failed is True
        assert stored.to_eval_score()["judge_failed"] is True

    async def test_non_uuid_incident_raises_before_judging(self) -> None:
        client = FakeJudgeClient()
        episodic = _episodic()
        with pytest.raises(EvalError, match="not a UUID"):
            await score_incident(
                _trajectory("escalated"),
                _sample_ground_truth(),
                incident_id="eval-x",
                scenario_id="s",
                client=client,
                model="openai/gpt-4.1",
                episodic=episodic,
            )
        assert client.calls == []
        episodic.store_eval_score.assert_not_awaited()

    async def test_missing_incident_row_is_logged_and_still_pushed(self) -> None:
        with (
            patch("sentinel.eval.judge.push_score") as push,
            patch("sentinel.eval.judge.logger") as log,
        ):
            await score_incident(
                _trajectory("escalated"),
                _sample_ground_truth(),
                incident_id=_INC,
                scenario_id="s",
                client=FakeJudgeClient(),
                model="openai/gpt-4.1",
                episodic=_episodic(stored=False),
            )
        assert "eval_score_no_incident_row" in [c.args[0] for c in log.warning.call_args_list]
        assert push.call_count == 5

    async def test_store_failure_propagates_and_nothing_is_pushed(self) -> None:
        episodic = _episodic()
        episodic.store_eval_score.side_effect = SentinelMemoryError("x")
        with patch("sentinel.eval.judge.push_score") as push, pytest.raises(SentinelMemoryError):
            await score_incident(
                _trajectory("escalated"),
                _sample_ground_truth(),
                incident_id=_INC,
                scenario_id="s",
                client=FakeJudgeClient(),
                model="openai/gpt-4.1",
                episodic=episodic,
            )
        push.assert_not_called()
