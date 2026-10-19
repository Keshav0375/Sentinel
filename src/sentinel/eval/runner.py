"""Batch eval runner — runs every scenario through the pipeline and judges it.

Usage (library)::

    from sentinel.eval.runner import run_eval

    scores = await run_eval(scenario_ids=["bad_deploy_01", "db_pool_01"])

Usage (CLI)::

    python scripts/run_eval.py
    python scripts/run_eval.py --scenarios bad_deploy_01 db_pool_01
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# asyncpg ships no type stubs (see infra/database.py); only its Pool type is named here.
import asyncpg  # pyright: ignore[reportMissingTypeStubs]

from sentinel.agents.orchestrator import PIPELINE_ERROR, PipelineDeps, run_pipeline
from sentinel.config import Settings, get_settings
from sentinel.eval.judge import evaluate_trajectory, make_judge_client
from sentinel.generator.alert_gen import generate_alert, list_scenarios, load_scenario
from sentinel.generator.scenarios import Scenario
from sentinel.infra.database import close_pool, init_pool_from_settings
from sentinel.infra.db import create_tables
from sentinel.infra.logging import configure_logging, get_logger
from sentinel.infra.tracing import SentinelTracer, install_trace_processors
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.eval_result import TrajectoryScore
from sentinel.models.incident import IncidentStatus
from sentinel.models.pipeline import PipelineInput
from sentinel.providers import apply_sdk_defaults

logger = get_logger("sentinel.eval.runner")


@dataclass
class EvalRunResult:
    """Outcome of a single scenario in the batch eval."""

    scenario_id: str
    incident_id: str
    score: TrajectoryScore
    pipeline_error: str | None = None
    trajectory_missing: bool = False


async def run_scenario_eval(
    scenario: Scenario,
    *,
    short_term_memory: ShortTermMemory,
    trajectories_dir: Path,
    settings: Settings,
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    pool: asyncpg.Pool | None = None,
) -> EvalRunResult:
    """Run one scenario end-to-end and return a scored result.

    Runs the orchestrator pipeline on the scenario's alert (it builds its agents per
    run), reads the trajectory written by SentinelTracer, and calls the LLM judge.

    Args:
        scenario: Loaded scenario (with ground truth).
        short_term_memory: Shared STM — keyed by incident_id so concurrent
            scenarios don't collide.
        trajectories_dir: Directory where SentinelTracer writes trajectory JSON.
        settings: Validated Settings — selects the primary provider (the judge runs on
            the other one) and supplies the provider credentials.
        semantic_memory: Service metadata store.
        episodic_memory: Past-incident similarity store.
        pool: asyncpg pool ``get_deploy_details`` reads; None without ``DATABASE_URL``.

    Returns:
        ``EvalRunResult`` with scenario_id, incident_id, score, and error info.
    """
    scenario_id = scenario.scenario_id
    alert = generate_alert(scenario)
    # A UUID, as every incident id is (incidents.id; the LangFuse trace id is its hex).
    incident_id = str(uuid.uuid4())

    logger.info(
        "eval_scenario_start",
        scenario_id=scenario_id,
        incident_id=incident_id,
    )

    # The judge runs on a model no role uses, off the primary provider (§4.7, decision
    # 2026-10-18).
    judge_model, judge_client = make_judge_client(settings)

    # ── Set up incident in STM ─────────────────────────────────────────────────
    short_term_memory.create(incident_id, alert)

    # ── Run pipeline ───────────────────────────────────────────────────────────
    # run_pipeline never raises: a failure inside it is an escalated result.
    deps = PipelineDeps(
        settings=settings,
        semantic_memory=semantic_memory,
        episodic_memory=episodic_memory,
        pool=pool,
        # A synthetic run is not a past incident: search_past_incidents must not recall it.
        persist=False,
    )
    result = await run_pipeline(PipelineInput.from_alert(incident_id, alert), deps)
    pipeline_error: str | None = None
    reason = result.escalation.missing[0] if result.escalation and result.escalation.missing else ""
    if reason.startswith(PIPELINE_ERROR):
        pipeline_error = reason
        logger.error(
            "eval_pipeline_error",
            incident_id=incident_id,
            scenario_id=scenario_id,
            error=pipeline_error,
        )
    else:
        logger.info("eval_pipeline_complete", incident_id=incident_id, status=result.status)
    try:
        short_term_memory.update_context(
            incident_id,
            status=IncidentStatus(result.status),
            result=result.model_dump(mode="json"),
        )
    except KeyError:
        pass

    # ── Load trajectory from disk ──────────────────────────────────────────────
    traj_path = trajectories_dir / f"{incident_id}.json"
    trajectory: dict[str, Any] = {}
    trajectory_missing = False

    if traj_path.exists():
        try:
            trajectory = json.loads(traj_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "eval_trajectory_load_error",
                path=str(traj_path),
                error=str(exc),
            )
            trajectory_missing = True
    else:
        trajectory_missing = True
        if pipeline_error is None:
            logger.warning(
                "eval_trajectory_missing",
                path=str(traj_path),
                incident_id=incident_id,
            )

    # Enrich trajectory with the outcome and the STM context for the judge
    trajectory["pipeline_result"] = result.model_dump(mode="json")
    try:
        ctx = short_term_memory.get_context(incident_id)
        trajectory["stm_context"] = {
            "status": str(ctx.get("status", "")),
            "timeline": [
                {
                    "agent": e.agent_name,
                    "action": e.action,
                    "result": e.result_summary,
                }
                for e in ctx.get("timeline", [])
            ],
        }
    except KeyError:
        pass

    # ── Judge the trajectory ───────────────────────────────────────────────────
    # The trajectory carries result.models_used: evaluate_trajectory refuses to grade a run
    # that used the judge's model and returns a score marked judge_conflict (runtime guard,
    # decision 2026-10-18); aggregates below and in eval.report drop it.
    ground_truth = scenario.ground_truth.model_dump()
    score = await evaluate_trajectory(
        trajectory,
        ground_truth,
        incident_id=incident_id,
        scenario_id=scenario_id,
        client=judge_client,
        model=judge_model,
    )

    logger.info(
        "eval_scenario_complete",
        scenario_id=scenario_id,
        incident_id=incident_id,
        total_score=score.total_score,
        judge_conflict=score.judge_conflict,
    )
    return EvalRunResult(
        scenario_id=scenario_id,
        incident_id=incident_id,
        score=score,
        pipeline_error=pipeline_error,
        trajectory_missing=trajectory_missing,
    )


async def run_eval(
    scenario_ids: list[str] | None = None,
    *,
    db_path: Path | None = None,
    trajectories_dir: Path | None = None,
) -> list[EvalRunResult]:
    """Run the full eval suite and return per-scenario results.

    Iterates over all scenarios (or a specified subset). Each scenario:
    1. Runs the full agent pipeline
    2. Reads the trajectory JSON written by SentinelTracer
    3. Calls the LLM judge (evaluate_trajectory)

    Failures per scenario are logged and scored 0 — the batch always
    completes.

    Args:
        scenario_ids: Which scenarios to eval. ``None`` runs all 10.
        db_path: SQLite path override. Defaults to config value.
        trajectories_dir: Directory for trajectory JSON files.
            Defaults to ``reports/trajectories/eval``.

    Returns:
        List of ``EvalRunResult`` in scenario order.
    """
    settings = get_settings()
    configure_logging(settings.sentinel_log_level)

    resolved_db = db_path or settings.sentinel_db_path
    resolved_traj = trajectories_dir or Path("reports/trajectories/eval")
    resolved_ids = scenario_ids or list_scenarios()

    logger.info(
        "eval_run_start",
        scenario_count=len(resolved_ids),
        primary_provider=settings.sentinel_primary_provider,
        db_path=str(resolved_db),
    )

    # ── Infrastructure setup ───────────────────────────────────────────────────
    await create_tables(resolved_db)
    try:
        from data.seed import seed  # noqa: PLC0415

        await seed(resolved_db, verbose=False)
    except ImportError:
        logger.warning("eval_seed_skipped", reason="data package not on Python path")

    # Postgres pool for semantic and episodic memory. None without DATABASE_URL: every
    # memory call then raises, and the memory tools report "memory unavailable".
    pool = await init_pool_from_settings(settings)
    try:
        embedding_client = EmbeddingClient.from_model_name(settings.sentinel_embedding_model)
        semantic_memory = SemanticMemory(pool)
        episodic_memory = EpisodicMemory(pool, embedding_client)
        short_term_memory = ShortTermMemory()

        apply_sdk_defaults(settings)

        # Register tracer once — it collects spans for all scenarios via incident_id.
        tracer = SentinelTracer(short_term_memory, trajectories_dir=resolved_traj)
        install_trace_processors(tracer)

        # ── Batch run ──────────────────────────────────────────────────────────
        results: list[EvalRunResult] = []

        for scenario_id in resolved_ids:
            try:
                scenario = load_scenario(scenario_id)
            except FileNotFoundError:
                logger.error(
                    "eval_scenario_not_found",
                    scenario_id=scenario_id,
                )
                continue

            try:
                result = await run_scenario_eval(
                    scenario,
                    short_term_memory=short_term_memory,
                    trajectories_dir=resolved_traj,
                    settings=settings,
                    semantic_memory=semantic_memory,
                    episodic_memory=episodic_memory,
                    pool=pool,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "eval_scenario_unexpected_error",
                    scenario_id=scenario_id,
                    error=str(exc),
                )
                continue

            results.append(result)
    finally:
        await close_pool()

    logger.info(
        "eval_run_complete",
        scenarios_run=len(results),
        scenarios_requested=len(resolved_ids),
        judge_conflicts=sum(1 for r in results if r.score.judge_conflict),
        avg_score=summary_stats(results)["avg_total"],
    )
    return results


def scores_from_results(results: list[EvalRunResult]) -> list[TrajectoryScore]:
    """Extract just the TrajectoryScore objects from EvalRunResults."""
    return [r.score for r in results]


def summary_stats(results: list[EvalRunResult]) -> dict[str, Any]:
    """Compute aggregate statistics from a list of EvalRunResults.

    Returns a dict with keys: ``avg_total``, ``pass_rate``, ``failure_rate``,
    ``judge_conflicts``, ``generated_at``. Pass threshold is 3.0 (60% of the 0-5 scale).
    A ``judge_conflict`` score is invalid (decision 2026-10-18): it is counted in
    ``judge_conflicts`` and left out of every other figure.
    """
    valid = [r for r in results if not r.score.judge_conflict]
    conflicts = len(results) - len(valid)
    if not valid:
        return {
            "avg_total": 0.0,
            "pass_rate": 0.0,
            "failure_rate": 0.0,
            "judge_conflicts": conflicts,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    total_sum = sum(r.score.total_score for r in valid)
    pass_count = sum(1 for r in valid if r.score.total_score >= 3.0)
    error_count = sum(1 for r in valid if r.pipeline_error is not None)

    n = len(valid)
    return {
        "avg_total": round(total_sum / n, 4),
        "pass_rate": round(pass_count / n, 4),
        "failure_rate": round(error_count / n, 4),
        "judge_conflicts": conflicts,
        "generated_at": datetime.now(UTC).isoformat(),
    }
