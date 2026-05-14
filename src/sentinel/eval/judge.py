"""LLM-as-judge implementation — scores a trajectory against ground truth.

Usage::

    from openai import AsyncOpenAI
    from sentinel.eval.judge import evaluate_trajectory

    score = await evaluate_trajectory(
        trajectory=trajectory_dict,
        ground_truth=ground_truth_dict,
        incident_id="inc-001",
        scenario_id="bad_deploy_01",
        client=groq_client,
        model="llama-3.1-8b-instant",
    )
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from openai import AsyncOpenAI

from sentinel.eval.rubric import JUDGE_SYSTEM_PROMPT, format_judge_input
from sentinel.infra.logging import get_logger
from sentinel.models.eval_result import DimensionScore, EvalDimension, TrajectoryScore

logger = get_logger("sentinel.eval.judge")

_RETRY_DELAYS = (1.0, 2.0, 4.0)
_DEFAULT_REASONING = "Not evaluated — judge failed to score this dimension."


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
    client: AsyncOpenAI,
    model: str = "llama-3.1-8b-instant",
) -> TrajectoryScore:
    """Score a single incident trajectory against known ground truth.

    Calls the judge LLM (Groq-compatible via AsyncOpenAI) with the eval rubric
    as the system prompt. Retries up to 3 times with exponential backoff on
    failure. If all retries fail, returns a zero-scored ``TrajectoryScore`` so
    the eval runner can continue with remaining scenarios.

    Args:
        trajectory: Full incident trajectory dict (timeline, tool calls,
            agent outputs). Typically loaded from the trajectory JSON written
            by ``SentinelTracer``.
        ground_truth: Scenario ground truth block containing ``severity``,
            ``affected_service``, ``root_cause_summary``, ``recommended_action``,
            and ``deploy_id``.
        incident_id: Incident ID being evaluated (for correlation in logs).
        scenario_id: Scenario ID being evaluated.
        client: Pre-configured ``AsyncOpenAI`` client (Groq-compatible).
        model: Judge model name. Defaults to ``llama-3.1-8b-instant`` — fast
            and cheap for structured scoring.

    Returns:
        ``TrajectoryScore`` with per-dimension scores, reasoning, and total.
    """
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
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.0,
                max_tokens=1024,
            )
            raw = response.choices[0].message.content or ""
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


def _zero_score(incident_id: str, scenario_id: str, model: str) -> TrajectoryScore:
    """Return a TrajectoryScore of 0 for all dimensions (judge failure fallback)."""
    return TrajectoryScore(
        incident_id=incident_id,
        scenario_id=scenario_id,
        dimension_scores=[
            DimensionScore(
                dimension=dim,
                score=0.0,
                reasoning="Judge failed — score defaulted to 0.",
            )
            for dim in EvalDimension
        ],
        total_score=0.0,
        judge_model=model,
    )
