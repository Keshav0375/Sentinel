"""LLM-as-judge implementation — scores a trajectory against ground truth.

Usage::

    from sentinel.eval.judge import evaluate_trajectory, make_judge_client

    model, client = make_judge_client(settings)   # the non-primary provider (§4.7)
    score = await evaluate_trajectory(
        trajectory=trajectory_dict,
        ground_truth=ground_truth_dict,
        incident_id="inc-001",
        scenario_id="bad_deploy_01",
        client=client,
        model=model,
    )
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from typing import Any, Protocol, cast

import litellm

from sentinel.config import Settings
from sentinel.eval.rubric import JUDGE_SYSTEM_PROMPT, format_judge_input
from sentinel.exceptions import ProviderUnavailableError
from sentinel.infra.logging import get_logger
from sentinel.models.eval_result import DimensionScore, EvalDimension, TrajectoryScore
from sentinel.providers.router import judge_model_string, provider_credential, provider_of

logger = get_logger("sentinel.eval.judge")

_RETRY_DELAYS = (1.0, 2.0, 4.0)
_DEFAULT_REASONING = "Not evaluated — judge failed to score this dimension."


class JudgeClient(Protocol):
    """A chat completion call: the judge's only dependency on an LLM provider."""

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Return the assistant message text for *messages* on *model*."""
        ...


class LitellmJudgeClient:
    """:class:`JudgeClient` over LiteLLM, so the judge runs on either §4.7 provider."""

    def __init__(self, credential: str) -> None:
        self._credential = credential

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> str:
        response: Any = await litellm.acompletion(  # pyright: ignore[reportUnknownMemberType]
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=self._credential,
        )
        content = response.choices[0].message.content
        return content if isinstance(content, str) else ""


def make_judge_client(settings: Settings) -> tuple[str, JudgeClient]:
    """The judge's model string and a client for it, credential read now.

    The judge runs on a model no agent role uses, on the provider the agents do not
    (decision 2026-10-18), and has no fallback: falling back would put it on the agents'
    provider.

    Raises:
        ProviderUnavailableError: The judge provider's credential is not set.
    """
    model = judge_model_string(settings.sentinel_primary_provider)
    credential = provider_credential(provider_of(model), settings)
    if credential is None:
        raise ProviderUnavailableError(
            f"the eval judge runs on {model!r}; set {provider_of(model).upper()}_API_KEY"
        )
    return model, LitellmJudgeClient(credential)


def is_judge_conflict(judge_model: str, models_used: Sequence[str]) -> bool:
    """True when the run being scored used the judge's own model (decision 2026-10-18).

    The static map check keeps the judge off every role's (primary, fallback); this is the
    runtime half — any model that actually ran, however it got chosen, is checked.
    """
    return judge_model in models_used


def _run_models_used(trajectory: dict[str, Any]) -> list[str]:
    """The ``models_used`` of the PipelineResult carried in *trajectory*, or []."""
    result: Any = trajectory.get("pipeline_result")
    if not isinstance(result, dict):
        return []
    used: Any = cast("dict[str, Any]", result).get("models_used")
    if not isinstance(used, list):
        return []
    return [str(m) for m in cast("list[Any]", used)]


def _strip_code_fences(text: str) -> str:
    """Remove markdown ```json ... ``` fences that LLMs sometimes add."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text


def _parse_judge_response(
    raw: str,
    model: str,
    incident_id: str,
    scenario_id: str,
) -> TrajectoryScore:
    """Parse the judge LLM's raw text output into a TrajectoryScore.

    Handles JSON wrapped in markdown code fences. Missing dimensions get a score
    of 0. Scores outside [0, 5] are clamped.
    """
    cleaned = _strip_code_fences(raw)
    data: dict[str, Any] = json.loads(cleaned)

    raw_scores: list[dict[str, Any]] = data.get("dimension_scores", [])

    # Index by dimension name for fast lookup
    scores_by_dim: dict[str, dict[str, Any]] = {
        entry["dimension"]: entry for entry in raw_scores if "dimension" in entry
    }

    dimension_scores: list[DimensionScore] = []
    for dim in EvalDimension:
        entry = scores_by_dim.get(str(dim))
        if entry is None:
            logger.warning(
                "judge_missing_dimension",
                dimension=str(dim),
                incident_id=incident_id,
            )
            dimension_scores.append(
                DimensionScore(
                    dimension=dim,
                    score=0.0,
                    reasoning=_DEFAULT_REASONING,
                )
            )
            continue

        raw_score = float(entry.get("score", 0))
        clamped = max(0.0, min(5.0, raw_score))
        dimension_scores.append(
            DimensionScore(
                dimension=dim,
                score=clamped,
                reasoning=str(entry.get("reasoning", _DEFAULT_REASONING)),
            )
        )

    total = (
        sum(ds.score for ds in dimension_scores) / len(dimension_scores)
        if dimension_scores
        else 0.0
    )
    # Clamp total_score to [0, 5] in case of floating point rounding
    total = max(0.0, min(5.0, total))

    return TrajectoryScore(
        incident_id=incident_id,
        scenario_id=scenario_id,
        dimension_scores=dimension_scores,
        total_score=round(total, 4),
        judge_model=model,
    )


async def evaluate_trajectory(
    trajectory: dict[str, Any],
    ground_truth: dict[str, Any],
    *,
    incident_id: str,
    scenario_id: str,
    client: JudgeClient,
    model: str,
) -> TrajectoryScore:
    """Score a single incident trajectory against known ground truth.

    Calls the judge LLM with the eval rubric as the system prompt. Retries up to 3
    times with exponential backoff on failure. If all retries fail, returns a
    zero-scored ``TrajectoryScore`` so the eval runner can continue with remaining
    scenarios.

    A run whose ``pipeline_result.models_used`` contains *model* is never judged: the judge
    would grade itself, so the result is a zero score marked ``judge_conflict`` (logged),
    which every aggregate excludes (decision 2026-10-18).

    Args:
        trajectory: Full incident trajectory dict (timeline, tool calls,
            agent outputs). Typically loaded from the trajectory JSON written
            by ``SentinelTracer``.
        ground_truth: Scenario ground truth block containing ``severity``,
            ``affected_service``, ``root_cause_summary``, ``recommended_action``,
            and ``deploy_id``.
        incident_id: Incident ID being evaluated (for correlation in logs).
        scenario_id: Scenario ID being evaluated.
        client: The judge's chat client (``make_judge_client``).
        model: Judge ``provider/model`` string (``make_judge_client``).

    Returns:
        ``TrajectoryScore`` with per-dimension scores, reasoning, and total.
    """
    models_used = _run_models_used(trajectory)
    if is_judge_conflict(model, models_used):
        logger.error(
            "judge_conflict",
            incident_id=incident_id,
            scenario_id=scenario_id,
            judge_model=model,
            models_used=models_used,
        )
        return _zero_score(incident_id, scenario_id, model, judge_conflict=True)

    user_message = format_judge_input(trajectory, ground_truth)

    last_exc: Exception | None = None
    for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
        try:
            logger.info(
                "judge_call",
                incident_id=incident_id,
                scenario_id=scenario_id,
                model=model,
                attempt=attempt,
            )
            raw = await client.complete(
                model=model,
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.0,
                max_tokens=1024,
            )
            score = _parse_judge_response(raw, model, incident_id, scenario_id)
            logger.info(
                "judge_complete",
                incident_id=incident_id,
                scenario_id=scenario_id,
                total_score=score.total_score,
            )
            return score

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            last_exc = exc
            logger.warning(
                "judge_parse_error",
                incident_id=incident_id,
                attempt=attempt,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "judge_api_error",
                incident_id=incident_id,
                attempt=attempt,
                error=str(exc),
            )

        if delay is not None:
            await asyncio.sleep(delay)

    # All retries exhausted — return a zero score so the runner continues
    logger.error(
        "judge_failed",
        incident_id=incident_id,
        scenario_id=scenario_id,
        error=str(last_exc),
    )
    return _zero_score(incident_id, scenario_id, model)


def _zero_score(
    incident_id: str, scenario_id: str, model: str, *, judge_conflict: bool = False
) -> TrajectoryScore:
    """Return a TrajectoryScore of 0 for all dimensions (judge failure or conflict)."""
    reasoning = (
        "Judge conflict — the run used the judge's model; score invalid."
        if judge_conflict
        else "Judge failed — score defaulted to 0."
    )
    return TrajectoryScore(
        incident_id=incident_id,
        scenario_id=scenario_id,
        dimension_scores=[
            DimensionScore(
                dimension=dim,
                score=0.0,
                reasoning=reasoning,
            )
            for dim in EvalDimension
        ],
        total_score=0.0,
        judge_model=model,
        judge_conflict=judge_conflict,
    )
