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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents import Runner, add_trace_processor
from agents import trace as agent_trace
from openai import AsyncOpenAI

from sentinel.agents.comms import build_comms_agent
from sentinel.agents.deploy_correlator import build_deploy_correlator_agent
from sentinel.agents.log_analyst import build_log_analyst_agent
from sentinel.agents.orchestrator import build_orchestrator_agent
from sentinel.agents.remediation import build_remediation_agent
from sentinel.agents.triage import build_triage_agent
from sentinel.config import Settings, get_settings
from sentinel.eval.judge import evaluate_trajectory
from sentinel.generator.alert_gen import generate_alert, list_scenarios, load_scenario
from sentinel.generator.scenarios import Scenario
from sentinel.infra.db import create_tables
from sentinel.infra.logging import configure_logging, get_logger
from sentinel.infra.tracing import SentinelTracer
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.eval_result import TrajectoryScore
from sentinel.models.incident import IncidentStatus
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


async def _auto_approve(display: str) -> tuple[str, str | None]:
    """HITL approval function for automated eval — always approves immediately."""
    logger.debug("eval_auto_approve", preview=display[:80])
    return ("approve", "Auto-approved by eval runner")


async def run_scenario_eval(
    scenario: Scenario,
    *,
    short_term_memory: ShortTermMemory,
    trajectories_dir: Path,
    settings: Settings,
    judge_model: str,
    max_turns: int = 15,
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
) -> EvalRunResult:
    """Run one scenario end-to-end and return a scored result.

    Builds a fresh agent set with the scenario injected into log/deploy tools,
    runs the pipeline, reads the trajectory written by SentinelTracer, and
    calls the LLM judge.

    HITL is auto-approved so the batch runs without human intervention.

    Args:
        scenario: Loaded scenario (with ground truth).
        short_term_memory: Shared STM — keyed by incident_id so concurrent
            scenarios don't collide.
        trajectories_dir: Directory where SentinelTracer writes trajectory JSON.
        settings: Validated Settings — used for provider resolution.
        judge_model: Bare model name for the eval judge (Groq-compatible).
        max_turns: Tool-call cap per pipeline run.
        semantic_memory: Service metadata store.
        episodic_memory: Past-incident similarity store.

    Returns:
        ``EvalRunResult`` with scenario_id, incident_id, score, and error info.
    """
    scenario_id = scenario.scenario_id
    alert = generate_alert(scenario)
    incident_id = f"eval-{scenario_id}-{alert.alert_id[:8]}"

    logger.info(
        "eval_scenario_start",
        scenario_id=scenario_id,
        incident_id=incident_id,
    )

    # ── Build fresh agents with scenario injected ──────────────────────────────
    triage_agent = build_triage_agent(
        semantic_memory,
        episodic_memory,
        model_string=settings.sentinel_triage_model,
        settings=settings,
    )
    log_analyst_agent = build_log_analyst_agent(
        semantic_memory,
        scenario,
        model_string=settings.sentinel_analysis_model,
        settings=settings,
    )
    deploy_correlator_agent = build_deploy_correlator_agent(
        scenario,
        model_string=settings.sentinel_triage_model,
        settings=settings,
    )
    remediation_agent = build_remediation_agent(
        model_string=settings.sentinel_analysis_model,
        settings=settings,
        approval_fn=_auto_approve,
    )
    comms_agent = build_comms_agent(
        model_string=settings.sentinel_triage_model,
        settings=settings,
    )
    orchestrator = build_orchestrator_agent(
        triage_agent,
        log_analyst_agent,
        deploy_correlator_agent,
        remediation_agent,
        comms_agent,
        model_string=settings.sentinel_analysis_model,
        settings=settings,
    )

    # Judge uses Groq directly (not LiteLLM) — strip provider prefix if present.
    groq_client = AsyncOpenAI(
        api_key=settings.groq_api_key,
        base_url=settings.groq_base_url,
    )

    # ── Set up incident in STM ─────────────────────────────────────────────────
    short_term_memory.create(incident_id, alert)

    # ── Run pipeline ───────────────────────────────────────────────────────────
    input_text = f"New incident ID: {incident_id}\nAlert payload: {alert.model_dump_json()}"
    pipeline_error: str | None = None

    try:
        with agent_trace(
            "incident_pipeline",
            metadata={"incident_id": incident_id},
        ):
            await Runner.run(orchestrator, input_text, max_turns=max_turns)
        short_term_memory.update_context(incident_id, status=IncidentStatus.RESOLVED)
        logger.info("eval_pipeline_complete", incident_id=incident_id)
    except Exception as exc:  # noqa: BLE001
        pipeline_error = str(exc)
        logger.error(
            "eval_pipeline_error",
            incident_id=incident_id,
            scenario_id=scenario_id,
            error=pipeline_error,
        )
        try:
            short_term_memory.update_context(incident_id, status=IncidentStatus.ESCALATED)
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

    # Enrich trajectory with STM context for the judge
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
    ground_truth = scenario.ground_truth.model_dump()
    score = await evaluate_trajectory(
        trajectory,
        ground_truth,
        incident_id=incident_id,
        scenario_id=scenario_id,
        client=groq_client,
        model=judge_model,
    )

    logger.info(
        "eval_scenario_complete",
        scenario_id=scenario_id,
        incident_id=incident_id,
        total_score=score.total_score,
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
    judge_model: str | None = None,
) -> list[EvalRunResult]:
    """Run the full eval suite and return per-scenario results.

    Iterates over all scenarios (or a specified subset). Each scenario:
    1. Runs the full agent pipeline with auto-approve HITL
    2. Reads the trajectory JSON written by SentinelTracer
    3. Calls the LLM judge (evaluate_trajectory)

    Failures per scenario are logged and scored 0 — the batch always
    completes.

    Args:
        scenario_ids: Which scenarios to eval. ``None`` runs all 10.
        db_path: SQLite path override. Defaults to config value.
        trajectories_dir: Directory for trajectory JSON files.
            Defaults to ``reports/trajectories/eval``.
        judge_model: Override judge model. Defaults to config value.

    Returns:
        List of ``EvalRunResult`` in scenario order.
    """
    settings = get_settings()
    configure_logging(settings.sentinel_log_level)

    resolved_db = db_path or settings.sentinel_db_path
    resolved_traj = trajectories_dir or Path("reports/trajectories/eval")
    resolved_judge = judge_model or settings.sentinel_judge_model
    resolved_ids = scenario_ids or list_scenarios()

    logger.info(
        "eval_run_start",
        scenario_count=len(resolved_ids),
        judge_model=resolved_judge,
        db_path=str(resolved_db),
    )

    # ── Infrastructure setup ───────────────────────────────────────────────────
    await create_tables(resolved_db)
    try:
        from data.seed import seed  # noqa: PLC0415

        await seed(resolved_db, verbose=False)
    except ImportError:
        logger.warning("eval_seed_skipped", reason="data package not on Python path")

    embedding_client = EmbeddingClient.from_model_name(settings.sentinel_embedding_model)
    semantic_memory = SemanticMemory(resolved_db)
    episodic_memory = EpisodicMemory(resolved_db, embedding_client)
    short_term_memory = ShortTermMemory()

    apply_sdk_defaults(settings)

    # Register tracer once — it collects spans for all scenarios via incident_id.
    tracer = SentinelTracer(short_term_memory, trajectories_dir=resolved_traj)
    add_trace_processor(tracer)

    # ── Batch run ──────────────────────────────────────────────────────────────
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
                judge_model=resolved_judge,
                max_turns=settings.sentinel_max_tool_calls + 5,
                semantic_memory=semantic_memory,
                episodic_memory=episodic_memory,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "eval_scenario_unexpected_error",
                scenario_id=scenario_id,
                error=str(exc),
            )
            continue

        results.append(result)

    logger.info(
        "eval_run_complete",
        scenarios_run=len(results),
        scenarios_requested=len(resolved_ids),
        avg_score=(sum(r.score.total_score for r in results) / len(results) if results else 0.0),
    )
    return results


def scores_from_results(results: list[EvalRunResult]) -> list[TrajectoryScore]:
    """Extract just the TrajectoryScore objects from EvalRunResults."""
    return [r.score for r in results]


def summary_stats(results: list[EvalRunResult]) -> dict[str, Any]:
    """Compute aggregate statistics from a list of EvalRunResults.

    Returns a dict with keys: ``avg_total``, ``pass_rate``, ``failure_rate``,
    ``generated_at``. Pass threshold is 3.0 (60% of the 0-5 scale).
    """
    if not results:
        return {
            "avg_total": 0.0,
            "pass_rate": 0.0,
            "failure_rate": 0.0,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    total_sum = sum(r.score.total_score for r in results)
    pass_count = sum(1 for r in results if r.score.total_score >= 3.0)
    error_count = sum(1 for r in results if r.pipeline_error is not None)

    n = len(results)
    return {
        "avg_total": round(total_sum / n, 4),
        "pass_rate": round(pass_count / n, 4),
        "failure_rate": round(error_count / n, 4),
        "generated_at": datetime.now(UTC).isoformat(),
    }
