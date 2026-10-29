"""FastAPI app entrypoint — lifespan, dependency wiring, and router mounting.

Run with::

    uvicorn sentinel.main:app --reload
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

from sentinel.agents.orchestrator import ORCHESTRATOR, PipelineDeps, ResumePoint, run_pipeline
from sentinel.api.auth import configure_auth, require_incident_write
from sentinel.api.config_view import make_config_router
from sentinel.api.eval_report import make_eval_report_router
from sentinel.api.generate import make_generate_router
from sentinel.api.health import database_configured, db_readiness, make_health_router
from sentinel.api.incidents import make_incidents_router
from sentinel.api.scenarios import make_scenarios_router
from sentinel.api.trajectories import make_trajectories_router
from sentinel.api.webhooks import make_incident_router
from sentinel.config import STM_TTL_MARGIN_S, Settings, get_settings
from sentinel.eval.dataset_writer import DATASET_WRITES
from sentinel.infra.dashboard_emitter import DashboardEventEmitter
from sentinel.infra.database import close_pool, init_pool_from_settings
from sentinel.infra.event_bus import EventBus, EventType, PipelineEvent
from sentinel.infra.incident_resume import IncidentResumer, IncidentRunFn
from sentinel.infra.incident_tasks import IncidentTasks
from sentinel.infra.logging import configure_logging, get_logger
from sentinel.infra.redis_client import RedisHandle, build_redis
from sentinel.infra.status_events import StatusPublisher
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
from sentinel.memory.cache import Cache, PostgresCache, build_cache
from sentinel.memory.checkpoints import (
    INCIDENT_IN_PROGRESS,
    CheckpointStore,
    CheckpointWriter,
)
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.memory.short_term import RedisStmMirror, ShortTermMemory
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
)


def make_pipeline_fn(
    settings_fn: Callable[[], Settings],
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    short_term_memory: ShortTermMemory,
    event_bus: EventBus | None = None,
    pool: asyncpg.Pool | None = None,
    checkpoints: CheckpointWriter | None = None,
    cache: Cache | None = None,
    publisher: StatusPublisher | None = None,
) -> IncidentRunFn:
    """Build the async pipeline callback fired by the webhook for each new incident.

    Each incident runs the Python orchestrator (``agents/orchestrator.run_pipeline``),
    which builds its agents per run, so provider credentials are read at use (§4.7), never
    at boot. On completion the incident's short-term memory gets the terminal status
    (``rollback_proposed`` | ``escalated``) and the result, and the dashboard stream is
    closed with an ``incident_completed`` event. The dataset item is written in the
    background and never delays this.

    Args:
        settings_fn: Callable returning the current Settings.
        semantic_memory: Shared semantic memory for service lookups.
        episodic_memory: Shared episodic memory for past incident search.
        short_term_memory: Live STM — updated with terminal status on completion.
        event_bus: Optional EventBus for dashboard streaming.
        pool: asyncpg pool for ``get_deploy_details`` and the rollback-target check; None
            without ``DATABASE_URL`` (the tool then reports "unavailable" and the
            pipeline can only escalate).
        checkpoints: The checkpoint writer (placeholder row + stage checkpoints); None
            without a database.
        cache: The tool / LLM result cache; None caches nothing.
        publisher: Publishes ``in_progress`` when a new incident starts and its terminal
            status when it ends (a no-op without Redis).

    Returns:
        Async callable ``(PipelineInput, resume=None) → None`` suitable for passing to
        ``make_incident_router`` as ``pipeline_fn``; the resumer passes *resume*.
    """

    async def run_incident(request: PipelineInput, resume: ResumePoint | None = None) -> None:
        incident_id = request.incident_id
        status = IncidentStatus.ESCALATED
        result: PipelineResult | None = None
        if publisher is not None and resume is None:
            publisher.publish(incident_id, INCIDENT_IN_PROGRESS)
        try:
            deps = PipelineDeps(
                settings=settings_fn(),
                semantic_memory=semantic_memory,
                episodic_memory=episodic_memory,
                pool=pool,
                checkpoints=checkpoints,
                cache=cache,
            )
            # run_pipeline never raises: a failure inside it is an escalated result.
            result = await run_pipeline(request, deps, resume=resume)
            status = IncidentStatus(result.status)
        except Exception as exc:
            logger.error("pipeline_error", incident_id=incident_id, error=str(exc))
        if publisher is not None:
            publisher.publish(incident_id, status.value)

        try:
            short_term_memory.update_context(
                incident_id,
                status=status,
                result=None if result is None else result.model_dump(mode="json"),
            )
        except KeyError:
            pass  # the incident was cleared mid-pipeline
        if event_bus is not None:
            try:
                await event_bus.publish(
                    incident_id,
                    PipelineEvent(
                        type=EventType.INCIDENT_COMPLETED,
                        agent_name=ORCHESTRATOR,
                        incident_id=incident_id,
                        data={"status": status.value},
                    ),
                )
            except Exception as exc:
                logger.error(
                    "incident_completed_event_failed",
                    incident_id=incident_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            try:
                await event_bus.close_incident(incident_id)
            except Exception as exc:
                logger.error(
                    "incident_stream_close_failed",
                    incident_id=incident_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

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
    6. Build the Redis handle (``REDIS_HOST``; connects nothing), the cache, the checkpoint
       writer (with a pool), the status publisher and the STM mirror (with Redis).
    7. Wire memory clients, the SDK defaults, the pipeline callback and the routers; then,
       with a pool, sweep the stuck ``in_progress`` incidents (one resume each) and purge
       the expired cache rows in a background task that never delays ``/ready``.

    Teardown (each step runs even if the one before it raised or was cancelled): in-flight
    incidents are drained (up to SENTINEL_SHUTDOWN_GRACE_S; the rest are logged, then
    cancelled) BEFORE the traces flush and the pool closes, so a finishing incident can
    still store itself; then the background dataset writes (within the rest of the same
    grace; the rest cancelled), then the checkpoint writes, STM mirror writes and status
    events (same grace) and the Redis client; then the LangFuse client is shut down
    (flushing pending spans, for at most ``TRACE_SHUTDOWN_TIMEOUT_S``, after which it is
    abandoned so a failed startup still exits and the pod restarts), the pool is closed,
    then the Azure credential (the pool's password callable uses it) is closed.
    """
    settings = get_settings()
    configure_logging(settings.sentinel_log_level)
    auth_configured = configure_auth(settings)
    credential = LazyAzureCredential()
    incident_tasks = IncidentTasks(settings.sentinel_max_concurrent_pipelines)
    grace_s = settings.sentinel_shutdown_grace_s
    redis: RedisHandle | None = None
    checkpoint_writer: CheckpointWriter | None = None
    stm_mirror: RedisStmMirror | None = None
    publisher: StatusPublisher | None = None
    background: set[asyncio.Task[None]] = set()
    try:
        settings = await load_key_vault_secrets(settings, credential)
        langfuse_enabled = configure_langfuse()
        # Opened inside the try: a failure anywhere later in startup still closes it, so a
        # failed startup leaves no open connections and no stale module-level pool.
        pool = await init_pool_from_settings(
            settings, password_factory=lambda: entra_token_password(credential.get())
        )
        redis = build_redis(settings, credential)
        cache = build_cache(settings, pool, redis)
        checkpoint_writer = CheckpointWriter(CheckpointStore(pool)) if pool is not None else None
        publisher = StatusPublisher(redis)
        stm_mirror = (
            RedisStmMirror(redis, int(settings.sentinel_pipeline_timeout_s) + STM_TTL_MARGIN_S)
            if redis is not None
            else None
        )
        logger.info(
            "sentinel_startup",
            langfuse_enabled=langfuse_enabled,
            auth_configured=auth_configured,
            key_vault=settings.key_vault_url is not None,
            dev_open_dashboard=settings.sentinel_dev_open_dashboard,
            redis_configured=redis is not None,
            cache_enabled=cache is not None,
        )
        resumer = _wire_app(
            app,
            settings,
            pool,
            incident_tasks,
            redis=redis,
            cache=cache,
            checkpoints=checkpoint_writer,
            publisher=publisher,
            stm_mirror=stm_mirror,
        )
        if pool is not None:
            task = asyncio.create_task(
                _after_startup(resumer, cache), name="sentinel-startup-sweep"
            )
            background.add(task)
            task.add_done_callback(background.discard)
        logger.info("sentinel_ready")
        yield
    finally:
        # Each step can raise or be cancelled (e.g. a second SIGTERM); the nested finally
        # blocks run the later ones regardless. The drain comes first: a finishing incident
        # still needs the pool and its traces. Flushing can block on network I/O: it runs
        # off the loop and shutdown_traces bounds it (TRACE_SHUTDOWN_TIMEOUT_S). The dataset
        # writes drain next, inside what is left of the same grace (they need the LangFuse
        # client, so before the flush).
        loop = asyncio.get_running_loop()
        drain_deadline = loop.time() + grace_s

        def remaining() -> float:
            return max(0.0, drain_deadline - loop.time())

        try:
            try:
                for task in list(background):
                    task.cancel()
                await incident_tasks.shutdown(grace_s)
            finally:
                try:
                    await DATASET_WRITES.drain(remaining())
                finally:
                    await _drain_memory(remaining, checkpoint_writer, stm_mirror, publisher, redis)
        finally:
            try:
                await asyncio.to_thread(shutdown_traces)
            finally:
                try:
                    await close_pool()
                finally:
                    credential.close()
                    logger.info("sentinel_shutdown")


async def _after_startup(resumer: IncidentResumer | None, cache: Cache | None) -> None:
    """The startup sweep of stuck incidents, then the purge of expired cache rows; both
    best effort, never raising."""
    if resumer is not None:
        await resumer.sweep()
    if isinstance(cache, PostgresCache):
        purged = await cache.purge_expired()
        logger.info("cache_purged", expired=purged)


async def _drain_memory(
    remaining: Callable[[], float],
    checkpoints: CheckpointWriter | None,
    stm_mirror: RedisStmMirror | None,
    publisher: StatusPublisher | None,
    redis: RedisHandle | None,
) -> None:
    """Drain the background memory writes within what is left of the grace, then close
    Redis. Each step runs even if the one before it raised."""
    try:
        if checkpoints is not None:
            await checkpoints.drain(remaining())
    finally:
        try:
            if stm_mirror is not None:
                await stm_mirror.drain(remaining())
        finally:
            try:
                if publisher is not None:
                    await publisher.drain(remaining())
            finally:
                if redis is not None:
                    await redis.aclose()


def _wire_app(
    app: FastAPI,
    settings: Settings,
    pool: asyncpg.Pool | None,
    incident_tasks: IncidentTasks,
    *,
    redis: RedisHandle | None = None,
    cache: Cache | None = None,
    checkpoints: CheckpointWriter | None = None,
    publisher: StatusPublisher | None = None,
    stm_mirror: RedisStmMirror | None = None,
) -> IncidentResumer | None:
    """Build the memory clients, SDK defaults, tracing and routers, and mount them.

    *pool* is None when no ``DATABASE_URL`` is configured: semantic and episodic memory
    are still built, and every call to them raises ``SentinelMemoryError`` ("memory
    unavailable"). *incident_tasks* is the lifespan's in-flight registry: the webhook
    router caps and tracks incidents with it, and the lifespan drains it at shutdown.

    *redis* (None without ``REDIS_HOST``) and what is built on it — *cache*, *publisher*,
    *stm_mirror* — and the *checkpoints* writer (None without a pool) are the lifespan's;
    every one is optional. Returns the :class:`IncidentResumer` (None without checkpoints)
    the lifespan sweeps with.

    Only ``/health`` and ``/ready`` are open. Every other route requires an Entra bearer
    token with the ``Incident.Write`` role. With ``SENTINEL_DEV_OPEN_DASHBOARD`` the
    dashboard pages and their read APIs (``DEV_OPEN_ROUTES``) are open as well; the
    webhook, incidents and generate routes stay protected.
    """
    # ── Memory clients ────────────────────────────────────────────────────────
    embedding_client = EmbeddingClient.from_model_name(settings.sentinel_embedding_model)
    semantic_memory = SemanticMemory(pool)
    episodic_memory = EpisodicMemory(pool, embedding_client)
    short_term_memory = ShortTermMemory(mirror=stm_mirror)

    # ── LLM + SDK defaults ────────────────────────────────────────────────────
    apply_sdk_defaults(settings)

    # ── Event bus ─────────────────────────────────────────────────────────────
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
        checkpoints=checkpoints,
        cache=cache,
        publisher=publisher,
    )
    resumer: IncidentResumer | None = None
    if checkpoints is not None:
        resumer = IncidentResumer(
            CheckpointStore(pool),
            incident_tasks,
            pipeline_fn,
            lambda: PipelineDeps(
                settings=settings,
                semantic_memory=semantic_memory,
                episodic_memory=episodic_memory,
                pool=pool,
                checkpoints=checkpoints,
                cache=cache,
            ),
            short_term_memory,
            stm_mirror=stm_mirror,
            publisher=publisher,
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
        make_incident_router(
            short_term_memory,
            episodic_memory,
            pipeline_fn,
            tasks=incident_tasks,
            max_body_bytes=settings.sentinel_webhook_max_body_bytes,
            resumer=resumer,
        ),
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
    app.include_router(make_scenarios_router(), dependencies=dashboard)
    app.include_router(make_eval_report_router(), dependencies=dashboard)
    app.include_router(make_config_router(settings), dependencies=dashboard)
    app.include_router(make_trajectories_router(trajectories_dir), dependencies=dashboard)
    return resumer


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
