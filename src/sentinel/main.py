"""FastAPI app entrypoint — lifespan, dependency wiring, and router mounting.

Run with::

    uvicorn sentinel.main:app --reload

The app must be launched from the project root so that the ``data`` package
(scenario files) is importable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

# asyncpg ships no type stubs (see infra/database.py); only its Pool type is named here.
import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from fastapi import APIRouter, Depends, FastAPI, params
from fastapi.responses import HTMLResponse

from sentinel.agents.orchestrator import ORCHESTRATOR, PipelineDeps, run_pipeline
from sentinel.api.auth import configure_auth, require_incident_write
from sentinel.api.config_view import make_config_router
from sentinel.api.eval_report import make_eval_report_router
from sentinel.api.events import make_events_router
from sentinel.api.generate import make_generate_router
from sentinel.api.health import database_configured, db_readiness, make_health_router
from sentinel.api.incidents import make_incidents_router
from sentinel.api.scenarios import make_scenarios_router
from sentinel.api.trajectories import make_trajectories_router
from sentinel.api.webhooks import PipelineFn, make_incident_router
from sentinel.config import Settings, get_settings
from sentinel.infra.dashboard_emitter import DashboardEventEmitter
from sentinel.infra.database import close_pool, init_pool_from_settings
from sentinel.infra.event_bus import EventBus, EventType, PipelineEvent
from sentinel.infra.logging import configure_logging, get_logger
from sentinel.infra.tracing import (
    SentinelTracer,
    configure_langfuse,
    install_trace_processors,
    shutdown_traces,
)
from sentinel.infra.workload_identity import (
    LazyAzureCredential,
    entra_token_password,
    load_key_vault_secrets,
)
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.incident import IncidentStatus
from sentinel.models.pipeline import PipelineInput, PipelineResult
from sentinel.providers import apply_sdk_defaults

_DASHBOARD_HTML = Path(__file__).resolve().parent / "dashboard" / "index.html"
_EVAL_HTML = Path(__file__).resolve().parent / "dashboard" / "eval.html"

logger = get_logger("sentinel.main")

# Routes served without a token under SENTINEL_DEV_OPEN_DASHBOARD (used only in the warning log).
DEV_OPEN_ROUTES: Final[tuple[str, ...]] = (
    "GET /",
    "GET /eval",
    "GET /api/scenarios",
    "GET /api/config",
    "GET /api/trajectories",
    "GET /api/eval-report",
    "GET /events/{incident_id}",
)


def make_pipeline_fn(
    settings_fn: Callable[[], Settings],
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    short_term_memory: ShortTermMemory,
    event_bus: EventBus | None = None,
    pool: asyncpg.Pool | None = None,
) -> PipelineFn:
    """Build the async pipeline callback fired by the webhook for each new incident.

    Each incident runs the Python orchestrator (``agents/orchestrator.run_pipeline``),
    which builds its agents per run, so provider credentials are read at use (§4.7), never
    at boot. On completion the incident's short-term memory gets the terminal status
    (``rollback_proposed`` | ``escalated``) and the result, and the dashboard stream is
    closed with an ``incident_completed`` event.

    Args:
        settings_fn: Callable returning the current Settings.
        semantic_memory: Shared semantic memory for service lookups.
        episodic_memory: Shared episodic memory for past incident search.
        short_term_memory: Live STM — updated with terminal status on completion.
        event_bus: Optional EventBus for dashboard streaming.
        pool: asyncpg pool for ``get_deploy_details`` and the rollback-target check; None
            without ``DATABASE_URL`` (the tool then reports "unavailable" and the
            pipeline can only escalate).

    Returns:
        Async callable ``(PipelineInput) → None`` suitable for passing to
        ``make_incident_router`` as ``pipeline_fn``.
    """

    async def run_incident(request: PipelineInput) -> None:
        incident_id = request.incident_id
        status = IncidentStatus.ESCALATED
        result: PipelineResult | None = None
        try:
            deps = PipelineDeps(
                settings=settings_fn(),
                semantic_memory=semantic_memory,
                episodic_memory=episodic_memory,
                pool=pool,
            )
            # run_pipeline never raises: a failure inside it is an escalated result.
            result = await run_pipeline(request, deps)
            status = IncidentStatus(result.status)
        except Exception as exc:
            logger.error("pipeline_error", incident_id=incident_id, error=str(exc))

        try:
            short_term_memory.update_context(
                incident_id,
                status=status,
                result=None if result is None else result.model_dump(mode="json"),
            )
        except KeyError:
            pass  # the incident was cleared mid-pipeline
        if event_bus is not None:
            await event_bus.publish(
                incident_id,
                PipelineEvent(
                    type=EventType.INCIDENT_COMPLETED,
                    agent_name=ORCHESTRATOR,
                    incident_id=incident_id,
                    data={"status": status.value},
                ),
            )
            await event_bus.close_incident(incident_id)

    return run_incident


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan: build all singletons, mount routers, then yield.

    Startup order:
    1. Load config and configure structured logging.
    2. Configure the Entra bearer validator (never raises; unconfigured → 401 everywhere).
    3. Read the Key Vault secrets (when KEY_VAULT_URL is set) BEFORE LangFuse, whose
       client reads LANGFUSE_* from the environment at configure time.
    4. Configure LangFuse.
    5. Open the asyncpg pool: DATABASE_URL, else PGHOST/PGDATABASE/PGUSER with an Entra
       token as the password for every new connection.
    6. Wire memory clients, the SDK defaults, the pipeline callback and the routers.

    Teardown (each step runs even if the one before it raised or was cancelled): the
    LangFuse client is shut down (flushing pending spans), the pool is closed, then the
    Azure credential (the pool's password callable uses it) is closed.
    """
    settings = get_settings()
    configure_logging(settings.sentinel_log_level)
    auth_configured = configure_auth(settings)
    credential = LazyAzureCredential()
    try:
        settings = await load_key_vault_secrets(settings, credential)
        logger.info(
            "sentinel_startup",
            langfuse_enabled=configure_langfuse(),
            auth_configured=auth_configured,
            key_vault=settings.key_vault_url is not None,
            dev_open_dashboard=settings.sentinel_dev_open_dashboard,
        )
        # Opened inside the try: a failure anywhere later in startup still closes it, so a
        # failed startup leaves no open connections and no stale module-level pool.
        pool = await init_pool_from_settings(
            settings, password_factory=lambda: entra_token_password(credential.get())
        )
        _wire_app(app, settings, pool)
        logger.info("sentinel_ready")
        yield
    finally:
        # Flushing can block on network I/O, so it runs off the loop. shutdown_traces
        # never raises, but the await can still be cancelled (e.g. a second SIGTERM):
        # the nested finally blocks close the pool and the credential regardless.
        try:
            await asyncio.to_thread(shutdown_traces)
        finally:
            try:
                await close_pool()
            finally:
                credential.close()
                logger.info("sentinel_shutdown")


def _wire_app(app: FastAPI, settings: Settings, pool: asyncpg.Pool | None) -> None:
    """Build the memory clients, SDK defaults, tracing and routers, and mount them.

    *pool* is None when no ``DATABASE_URL`` is configured: semantic and episodic memory
    are still built, and every call to them raises ``SentinelMemoryError`` ("memory
    unavailable").

    Only ``/health`` and ``/ready`` are open. Every other route requires an Entra bearer
    token with the ``Incident.Write`` role. With ``SENTINEL_DEV_OPEN_DASHBOARD`` the
    dashboard pages and their read APIs (``DEV_OPEN_ROUTES``) are open as well; the
    webhook, incidents and generate routes stay protected.
    """
    # ── Memory clients ────────────────────────────────────────────────────────
    embedding_client = EmbeddingClient.from_model_name(settings.sentinel_embedding_model)
    semantic_memory = SemanticMemory(pool)
    episodic_memory = EpisodicMemory(pool, embedding_client)
    short_term_memory = ShortTermMemory()

    # ── LLM + SDK defaults ────────────────────────────────────────────────────
    apply_sdk_defaults(settings)

    # ── Event bus (dashboard SSE) ─────────────────────────────────────────────
    event_bus = EventBus()

    # ── Trajectory tracing ────────────────────────────────────────────────────
    trajectories_dir = Path("reports/trajectories")
    tracer = SentinelTracer(short_term_memory, trajectories_dir=trajectories_dir)

    # ── Dashboard event emitter ───────────────────────────────────────────────
    dashboard_emitter = DashboardEventEmitter(event_bus)

    # Exactly Sentinel's processors (+ the R14 LangFuse generation bridge): the SDK's
    # default OpenAI trace exporter is dropped (decision 2026-10-15).
    install_trace_processors(tracer, dashboard_emitter)

    # ── API wiring ────────────────────────────────────────────────────────────
    # Agents are built per-incident inside make_pipeline_fn.
    pipeline_fn = make_pipeline_fn(
        lambda: settings,
        semantic_memory,
        episodic_memory,
        short_term_memory,
        event_bus=event_bus,
        pool=pool,
    )

    protected: list[params.Depends] = [Depends(require_incident_write)]
    dashboard: list[params.Depends] = [] if settings.sentinel_dev_open_dashboard else protected
    if settings.sentinel_dev_open_dashboard:
        logger.warning(
            "dev_open_dashboard",
            open_routes=list(DEV_OPEN_ROUTES),
            effect="the dashboard pages and their read APIs serve WITHOUT auth: "
            "local use only, never in the pod",
        )
    app.include_router(
        make_health_router(db_readiness(pool, db_configured=database_configured(settings)))
    )  # open (§3.5)
    app.include_router(
        make_incident_router(short_term_memory, episodic_memory, pipeline_fn),
        dependencies=protected,
    )
    app.include_router(
        make_incidents_router(short_term_memory, episodic_memory), dependencies=protected
    )
    app.include_router(
        make_generate_router(episodic_memory=episodic_memory, pool=pool, settings=settings),
        dependencies=protected,
    )
    app.include_router(make_dashboard_router(), dependencies=dashboard)
    app.include_router(make_events_router(event_bus), dependencies=dashboard)
    app.include_router(make_scenarios_router(), dependencies=dashboard)
    app.include_router(make_eval_report_router(), dependencies=dashboard)
    app.include_router(make_config_router(settings), dependencies=dashboard)
    app.include_router(make_trajectories_router(trajectories_dir), dependencies=dashboard)


def make_dashboard_router() -> APIRouter:
    """Router for the dashboard pages: ``GET /`` (live incidents) and ``GET /eval``."""
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> str:  # pyright: ignore[reportUnusedFunction]
        """Serve the live incident dashboard."""
        return _DASHBOARD_HTML.read_text(encoding="utf-8")

    @router.get("/eval", response_class=HTMLResponse, include_in_schema=False)
    async def eval_page() -> str:  # pyright: ignore[reportUnusedFunction]
        """Serve the eval results page."""
        return _EVAL_HTML.read_text(encoding="utf-8")

    return router


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
    return application


app = create_app()
