"""FastAPI app entrypoint — lifespan, dependency wiring, and router mounting.

Run with::

    uvicorn sentinel.main:app --reload

The app must be launched from the project root so that the ``data`` package
(seed script, scenario files) is importable.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agents import Runner, add_trace_processor, set_default_openai_client
from agents import trace as agent_trace
from fastapi import FastAPI
from openai import AsyncOpenAI

from sentinel.agents.comms import build_comms_agent
from sentinel.agents.deploy_correlator import build_deploy_correlator_agent
from sentinel.agents.log_analyst import build_log_analyst_agent
from sentinel.agents.orchestrator import build_orchestrator_agent
from sentinel.agents.remediation import build_remediation_agent
from sentinel.agents.triage import build_triage_agent
from sentinel.api.events import (
    HitlGateRegistry,
    make_api_approval_fn,
    make_events_router,
    reset_current_incident,
    set_current_incident,
)
from sentinel.api.health import make_health_router
from sentinel.api.incidents import make_incidents_router
from sentinel.api.webhooks import AlertDeduplicator, PipelineFn, make_alert_router
from sentinel.config import get_settings
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

logger = get_logger("sentinel.main")


def make_pipeline_fn(
    orchestrator: Any,
    short_term_memory: ShortTermMemory,
    max_turns: int = 15,
    event_bus: EventBus | None = None,
) -> PipelineFn:
    """Build the async pipeline callback fired by the webhook for each new alert.

    Extracted from ``lifespan`` so it can be unit-tested by injecting a mock
    orchestrator without running the full app startup sequence.

    Args:
        orchestrator: The pre-built Orchestrator Agent (any context type).
        short_term_memory: Live STM — updated with terminal status on completion.
        max_turns: Maximum agent turns before the SDK raises an error. Mirrors
            ``sentinel_max_tool_calls`` from Settings.
        event_bus: Optional EventBus — when provided, the incident_id is set in
            ``_CURRENT_INCIDENT_ID`` so the dashboard approval_fn can resolve
            HITL gates, and an ``incident_resolved`` event is published on
            pipeline completion.

    Returns:
        Async callable ``(incident_id, alert) → None`` suitable for passing to
        ``make_alert_router`` as ``pipeline_fn``.
    """

    async def run_pipeline(incident_id: str, alert: AlertPayload) -> None:
        logger.info("pipeline_start", incident_id=incident_id, service=alert.service)
        # Propagate incident_id into the async context so the dashboard
        # approval_fn can create the correct HITL gate without an extra arg.
        token = set_current_incident(incident_id)
        try:
            input_text = (
                f"New incident ID: {incident_id}\n"
                f"Alert payload: {alert.model_dump_json()}"
            )
            # Wrap in a named trace so SentinelTracer can associate spans
            # with this incident via span.trace_metadata["incident_id"].
            with agent_trace(
                "incident_pipeline",
                metadata={"incident_id": incident_id},
            ):
                await Runner.run(orchestrator, input_text, max_turns=max_turns)
            logger.info("pipeline_complete", incident_id=incident_id)
            try:
                short_term_memory.update_context(
                    incident_id, status=IncidentStatus.RESOLVED
                )
            except KeyError:
                pass  # incident was cleared externally during pipeline
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
                short_term_memory.update_context(
                    incident_id, status=IncidentStatus.ESCALATED
                )
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

    # ── LLM client ────────────────────────────────────────────────────────────
    groq_client = AsyncOpenAI(
        api_key=settings.groq_api_key,
        base_url=settings.groq_base_url,
    )
    # Set as SDK default so Runner and any SDK-internal calls use Groq.
    set_default_openai_client(groq_client)

    # ── Specialist agents ─────────────────────────────────────────────────────
    triage_agent = build_triage_agent(
        semantic_memory,
        episodic_memory,
        groq_client=groq_client,
        model_name=settings.sentinel_triage_model,
    )
    log_analyst_agent = build_log_analyst_agent(
        semantic_memory,
        scenario=None,  # MVP: no pre-loaded scenario in production mode
        groq_client=groq_client,
        model_name=settings.sentinel_analysis_model,
    )
    deploy_correlator_agent = build_deploy_correlator_agent(
        scenario=None,
        groq_client=groq_client,
        model_name=settings.sentinel_triage_model,
    )
    event_bus = EventBus()
    gate_registry = HitlGateRegistry()
    api_approval_fn = make_api_approval_fn(event_bus, gate_registry)

    remediation_agent = build_remediation_agent(
        groq_client=groq_client,
        model_name=settings.sentinel_analysis_model,
        approval_fn=api_approval_fn,
    )
    comms_agent = build_comms_agent(
        groq_client=groq_client,
        model_name=settings.sentinel_triage_model,
    )

    # ── Orchestrator ──────────────────────────────────────────────────────────
    orchestrator = build_orchestrator_agent(
        triage_agent,
        log_analyst_agent,
        deploy_correlator_agent,
        remediation_agent,
        comms_agent,
        groq_client=groq_client,
        model_name=settings.sentinel_analysis_model,
    )

    # ── Trajectory tracing ────────────────────────────────────────────────────
    trajectories_dir = Path("reports/trajectories")
    tracer = SentinelTracer(short_term_memory, trajectories_dir=trajectories_dir)
    add_trace_processor(tracer)

    # ── API wiring ────────────────────────────────────────────────────────────
    deduplicator = AlertDeduplicator()
    pipeline_fn = make_pipeline_fn(
        orchestrator,
        short_term_memory,
        max_turns=settings.sentinel_max_tool_calls,
        event_bus=event_bus,
    )

    app.include_router(make_alert_router(deduplicator, short_term_memory, pipeline_fn))
    app.include_router(make_incidents_router(short_term_memory))
    app.include_router(make_health_router(short_term_memory))
    app.include_router(make_events_router(event_bus, gate_registry))

    logger.info("sentinel_ready")
    yield

    logger.info("sentinel_shutdown")


def create_app() -> FastAPI:
    """Create and return the FastAPI application.

    The app is not ready to serve traffic until the lifespan handler completes
    startup (memory clients, agents, and routers are all wired there).
    """
    return FastAPI(
        title="Sentinel",
        description="Autonomous DevOps incident response agent",
        version="0.1.0",
        lifespan=lifespan,
    )


app = create_app()
