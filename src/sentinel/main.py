"""FastAPI app entrypoint — lifespan, dependency wiring, and router mounting.

Run with::

    uvicorn sentinel.main:app --reload

The app must be launched from the project root so that the ``data`` package
(seed script, scenario files) is importable.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agents import Runner, add_trace_processor, handoff
from agents import trace as agent_trace
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from sentinel.agents.comms import build_comms_agent
from sentinel.agents.deploy_correlator import build_deploy_correlator_agent
from sentinel.agents.log_analyst import build_log_analyst_agent
from sentinel.agents.orchestrator import build_orchestrator_agent
from sentinel.agents.remediation import build_remediation_agent
from sentinel.agents.triage import build_triage_agent
from sentinel.api.config_view import RuntimeConfig, make_config_router
from sentinel.api.eval_report import make_eval_report_router
from sentinel.api.events import (
    HitlGateRegistry,
    make_api_approval_fn,
    make_events_router,
    reset_current_incident,
    set_current_incident,
)
from sentinel.api.health import make_health_router
from sentinel.api.incidents import make_incidents_router
from sentinel.api.scenarios import make_scenarios_router
from sentinel.api.trajectories import make_trajectories_router
from sentinel.api.webhooks import AlertDeduplicator, PipelineFn, make_alert_router
from sentinel.config import Settings, get_settings
from sentinel.generator.alert_gen import load_scenario
from sentinel.generator.scenarios import Scenario
from sentinel.infra.dashboard_emitter import DashboardEventEmitter
from sentinel.infra.db import create_tables
from sentinel.infra.event_bus import EventBus
from sentinel.infra.logging import configure_logging, get_logger
from sentinel.infra.tracing import SentinelTracer
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload
from sentinel.models.incident import IncidentStatus
from sentinel.providers import apply_sdk_defaults, get_capabilities
from sentinel.tools.hitl import ApprovalFn

_DASHBOARD_HTML = Path(__file__).resolve().parent / "dashboard" / "index.html"
_EVAL_HTML = Path(__file__).resolve().parent / "dashboard" / "eval.html"

logger = get_logger("sentinel.main")


def _build_orchestrator_for_scenario(
    scenario: Scenario | None,
    settings: Settings,
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    approval_fn: ApprovalFn | None = None,
) -> Any:
    """Build a full orchestrator pipeline with scenario data injected.

    Rebuilds all agents per-incident so the log/deploy tools have access to
    the scenario's synthetic data. The construction cost is negligible — it's
    just Python object creation, not LLM calls.
    """
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
        approval_fn=approval_fn,
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

    caps = get_capabilities(settings.sentinel_analysis_model)
    back = handoff(orchestrator)
    back.strict_json_schema = caps.strict_schemas
    back_handoff: Any = [back]
    for specialist in [
        triage_agent,
        log_analyst_agent,
        deploy_correlator_agent,
        remediation_agent,
        comms_agent,
    ]:
        specialist.handoffs = back_handoff

    return orchestrator


def make_pipeline_fn(
    settings_fn: Callable[[], Settings],
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    short_term_memory: ShortTermMemory,
    max_turns: int = 15,
    event_bus: EventBus | None = None,
    approval_fn: ApprovalFn | None = None,
) -> PipelineFn:
    """Build the async pipeline callback fired by the webhook for each new alert.

    Agents are rebuilt per-incident so scenario-based alerts get their synthetic
    log/deploy data injected into the tools.

    Args:
        settings_fn: Callable returning current Settings (supports runtime
            model overrides via RuntimeConfig).
        semantic_memory: Shared semantic memory for service lookups.
        episodic_memory: Shared episodic memory for past incident search.
        short_term_memory: Live STM — updated with terminal status on completion.
        max_turns: Maximum agent turns before the SDK raises an error.
        event_bus: Optional EventBus for dashboard streaming.
        approval_fn: Optional HITL callback for the remediation agent.

    Returns:
        Async callable ``(incident_id, alert) → None`` suitable for passing to
        ``make_alert_router`` as ``pipeline_fn``.
    """

    async def run_pipeline(incident_id: str, alert: AlertPayload) -> None:
        settings = settings_fn()
        logger.info("pipeline_start", incident_id=incident_id, service=alert.service)

        scenario_id = alert.metadata.get("scenario_id")
        scenario: Scenario | None = None
        if scenario_id:
            try:
                scenario = load_scenario(str(scenario_id))
            except FileNotFoundError:
                logger.warning("scenario_not_found", scenario_id=scenario_id)

        orchestrator = _build_orchestrator_for_scenario(
            scenario, settings, semantic_memory, episodic_memory, approval_fn
        )

        token = set_current_incident(incident_id)
        try:
            input_text = f"New incident ID: {incident_id}\nAlert payload: {alert.model_dump_json()}"
            with agent_trace(
                "incident_pipeline",
                metadata={"incident_id": incident_id},
            ):
                await Runner.run(orchestrator, input_text, max_turns=max_turns)
            logger.info("pipeline_complete", incident_id=incident_id)
            try:
                short_term_memory.update_context(incident_id, status=IncidentStatus.RESOLVED)
            except KeyError:
                pass
            if event_bus is not None:
                from sentinel.infra.event_bus import EventType, PipelineEvent  # noqa: PLC0415

                await event_bus.publish(
                    incident_id,
                    PipelineEvent(
                        type=EventType.INCIDENT_RESOLVED,
                        agent_name="orchestrator",
                        incident_id=incident_id,
                    ),
                )
                await event_bus.close_incident(incident_id)
        except Exception as exc:
            logger.error("pipeline_error", incident_id=incident_id, error=str(exc))
            try:
                short_term_memory.update_context(incident_id, status=IncidentStatus.ESCALATED)
            except KeyError:
                pass
            if event_bus is not None:
                await event_bus.close_incident(incident_id)
        finally:
            reset_current_incident(token)

    return run_pipeline


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan: build all singletons, mount routers, then yield.

    Startup order:
    1. Load config and configure structured logging.
    2. Create SQLite tables (idempotent).
    3. Seed service map and runbooks (idempotent INSERT OR REPLACE).
    4. Build memory clients (SemanticMemory, EpisodicMemory, ShortTermMemory).
    5. Create Groq AsyncOpenAI client; set as SDK default.
    6. Build specialist agents and orchestrator.
    7. Wire alert deduplicator and pipeline callback.
    8. Mount all API routers.
    """
    settings = get_settings()
    configure_logging(settings.sentinel_log_level)
    logger.info("sentinel_startup", db_path=str(settings.sentinel_db_path))

    # ── Database ──────────────────────────────────────────────────────────────
    await create_tables(settings.sentinel_db_path)

    try:
        from data.seed import seed  # noqa: PLC0415 — lazy import, data/ must be on path

        await seed(settings.sentinel_db_path, verbose=False)
    except ImportError:
        logger.warning("seed_skipped", reason="data package not on Python path")

    # ── Memory clients ────────────────────────────────────────────────────────
    embedding_client = EmbeddingClient.from_model_name(settings.sentinel_embedding_model)
    semantic_memory = SemanticMemory(settings.sentinel_db_path)
    episodic_memory = EpisodicMemory(settings.sentinel_db_path, embedding_client)
    short_term_memory = ShortTermMemory()

    # ── LLM + SDK defaults ────────────────────────────────────────────────────
    apply_sdk_defaults(settings)

    # ── Event bus + HITL ──────────────────────────────────────────────────────
    event_bus = EventBus()
    gate_registry = HitlGateRegistry()
    api_approval_fn = make_api_approval_fn(event_bus, gate_registry)

    # ── Trajectory tracing ────────────────────────────────────────────────────
    trajectories_dir = Path("reports/trajectories")
    tracer = SentinelTracer(short_term_memory, trajectories_dir=trajectories_dir)
    add_trace_processor(tracer)

    # ── Dashboard event emitter ───────────────────────────────────────────────
    dashboard_emitter = DashboardEventEmitter(event_bus)
    add_trace_processor(dashboard_emitter)

    # ── Runtime config (dashboard model switching) ─────────────────────────────
    runtime_config = RuntimeConfig(settings)

    # ── API wiring ────────────────────────────────────────────────────────────
    # Agents are built per-incident inside make_pipeline_fn so scenario-based
    # alerts get their synthetic log/deploy data injected into the tools.
    deduplicator = AlertDeduplicator()
    pipeline_fn = make_pipeline_fn(
        runtime_config.effective_settings,
        semantic_memory,
        episodic_memory,
        short_term_memory,
        max_turns=settings.sentinel_max_tool_calls + 5,
        event_bus=event_bus,
        approval_fn=api_approval_fn,
    )

    app.include_router(make_alert_router(deduplicator, short_term_memory, pipeline_fn))
    app.include_router(make_incidents_router(short_term_memory))
    app.include_router(make_health_router(short_term_memory))
    app.include_router(make_events_router(event_bus, gate_registry))
    app.include_router(make_scenarios_router())
    app.include_router(make_eval_report_router())
    app.include_router(make_config_router(runtime_config))
    app.include_router(make_trajectories_router(trajectories_dir))

    logger.info("sentinel_ready")
    yield

    logger.info("sentinel_shutdown")


def create_app() -> FastAPI:
    """Create and return the FastAPI application.

    The app is not ready to serve traffic until the lifespan handler completes
    startup (memory clients, agents, and routers are all wired there).
    """
    application = FastAPI(
        title="Sentinel",
        description="Autonomous DevOps incident response agent",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> str:  # pyright: ignore[reportUnusedFunction]
        """Serve the live incident dashboard."""
        return _DASHBOARD_HTML.read_text(encoding="utf-8")

    @application.get("/eval", response_class=HTMLResponse, include_in_schema=False)
    async def eval_page() -> str:  # pyright: ignore[reportUnusedFunction]
        """Serve the eval results page."""
        return _EVAL_HTML.read_text(encoding="utf-8")

    return application


app = create_app()
