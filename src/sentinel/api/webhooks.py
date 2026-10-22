"""Webhook receiver — ``POST /webhooks/incident`` (§3.1).

Accepts an :class:`~sentinel.models.alert.IncidentPayload`, answers ``202`` at once and runs
the pipeline as one ``asyncio`` task per incident. No auth here: the app mounts the router
behind ``require_incident_write`` (backend 5.5).

- **Correlation id.** The body's ``correlation_id`` (a UUID) is authoritative. An
  ``X-Correlation-ID`` header that differs is logged (``correlation_header_mismatch``), never
  rejected. Every 202 sets the response header to the body's id.
- **Dedupe (§3.1).** On ``correlation_id``: a 24 h in-memory cache first, then the stored
  incidents (``EpisodicMemory.find_incident_by_correlation_id``; an unavailable database is
  logged and treated as a miss). A duplicate answers 202 with the original ``incident_id``
  and ``status: "duplicate"`` and starts nothing.
- **One task per incident.** Created in the handler, outside any span (§4.5), with its own
  entry in the shared short-term memory. The shared :class:`IncidentTasks` keeps each task
  until it ends, logs a task failure with its incident_id, caps concurrency (429 +
  ``Retry-After``; a duplicate still answers 202 and a 429'd correlation_id is not
  remembered) and, at shutdown, answers 503 + ``Retry-After``. The pipeline persists its
  own outcome (``run_pipeline``).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.incident_tasks import IncidentTasks
from sentinel.infra.logging import get_logger
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import IncidentPayload
from sentinel.models.pipeline import PipelineInput

logger = get_logger("sentinel.api.webhooks")

# Type alias for the async pipeline callback injected at app startup.
PipelineFn = Callable[[PipelineInput], Awaitable[None]]

DEDUP_TTL_SECONDS: Final = 24 * 60 * 60.0
CORRELATION_HEADER: Final = "X-Correlation-ID"
ACCEPTED_MESSAGE: Final = "Agent pipeline triggered"
DUPLICATE_MESSAGE: Final = "Duplicate correlation_id: pipeline already triggered"
RETRY_AFTER_S: Final = 30
SHUTTING_DOWN_DETAIL: Final = "shutting down; retry later"


class IncidentAck(BaseModel):
    """The 202 body: the incident id, the correlation id, and whether it was a duplicate."""

    incident_id: str
    correlation_id: str
    status: Literal["accepted", "duplicate"]
    message: str


@dataclass
class _CacheEntry:
    """Single entry in the dedupe cache."""

    incident_id: str
    recorded_at: float  # clock() — monotonic seconds, immune to wall-clock changes


class CorrelationDeduplicator:
    """In-memory idempotency cache: correlation_id → incident_id, expiring after 24 h.

    The first line of the dedupe; the incidents table (looked up by the router) is the
    second, so a restart does not forget a stored incident.
    """

    TTL_SECONDS: float = DEDUP_TTL_SECONDS

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}

    def check(self, correlation_id: str) -> str | None:
        """The incident_id recorded for *correlation_id*, or None (evicts an expired entry)."""
        entry = self._cache.get(correlation_id)
        if entry is None:
            return None
        if self._clock() - entry.recorded_at >= self.TTL_SECONDS:
            del self._cache[correlation_id]
            return None
        return entry.incident_id

    def record(self, correlation_id: str, incident_id: str) -> None:
        """Remember *correlation_id* → *incident_id* (expired entries are dropped first)."""
        self.evict_expired()
        self._cache[correlation_id] = _CacheEntry(
            incident_id=incident_id, recorded_at=self._clock()
        )

    def evict_expired(self) -> int:
        """Remove every entry older than ``TTL_SECONDS``; returns how many."""
        now = self._clock()
        expired = [
            cid for cid, entry in self._cache.items() if now - entry.recorded_at >= self.TTL_SECONDS
        ]
        for cid in expired:
            del self._cache[cid]
        return len(expired)

    @property
    def size(self) -> int:
        """Current number of cache entries (including any not-yet-evicted expired ones)."""
        return len(self._cache)


def _same_uuid(a: str, b: str) -> bool:
    """True when both are UUIDs and equal; a non-UUID counts as different."""
    try:
        return uuid.UUID(a) == uuid.UUID(b)
    except ValueError:
        return False


async def _await(awaitable: Awaitable[None]) -> None:
    """Wrap an awaitable as the coroutine ``asyncio.create_task`` takes."""
    await awaitable


def make_incident_router(
    short_term_memory: ShortTermMemory,
    episodic_memory: EpisodicMemory,
    pipeline_fn: PipelineFn | None = None,
    *,
    deduplicator: CorrelationDeduplicator | None = None,
    tasks: IncidentTasks | None = None,
) -> APIRouter:
    """Create the incident webhook router.

    Args:
        short_term_memory: Shared ``ShortTermMemory``; each new incident gets its entry.
        episodic_memory: Looked up by correlation_id for the persistent half of the dedupe.
        pipeline_fn: Async callback ``(PipelineInput) → None`` that runs the orchestrator.
            When ``None`` the endpoint logs ``pipeline_not_wired`` and starts nothing.
        deduplicator: The in-memory cache (default: a fresh 24 h one).
        tasks: The shared in-flight registry (main passes the lifespan's; it caps
            concurrency and is drained at shutdown). Default: a fresh one.

    Returns:
        ``APIRouter`` with ``POST /webhooks/incident`` registered.
    """
    router = APIRouter()
    dedup = deduplicator if deduplicator is not None else CorrelationDeduplicator()
    running = tasks if tasks is not None else IncidentTasks()

    async def _stored_original(correlation_id: str) -> str | None:
        try:
            return await episodic_memory.find_incident_by_correlation_id(correlation_id)
        except SentinelMemoryError as exc:
            logger.warning(
                "dedupe_lookup_unavailable", correlation_id=correlation_id, error=str(exc)
            )
            return None

    def _duplicate(correlation_id: str, incident_id: str, source: str) -> IncidentAck:
        logger.info(
            "incident_deduplicated",
            correlation_id=correlation_id,
            incident_id=incident_id,
            source=source,
        )
        return IncidentAck(
            incident_id=incident_id,
            correlation_id=correlation_id,
            status="duplicate",
            message=DUPLICATE_MESSAGE,
        )

    @router.post("/webhooks/incident", status_code=202, response_model=IncidentAck)
    async def receive_incident(  # pyright: ignore[reportUnusedFunction]
        payload: IncidentPayload,
        response: Response,
        x_correlation_id: Annotated[str | None, Header()] = None,
    ) -> IncidentAck:
        """Accept an incident: dedupe on correlation_id, then start its pipeline task.

        Answers 202 immediately; the pipeline runs as its own task and persists its own
        outcome.
        """
        correlation_id = payload.correlation_id
        if x_correlation_id is not None and not _same_uuid(x_correlation_id, correlation_id):
            logger.warning(
                "correlation_header_mismatch",
                header=x_correlation_id,
                correlation_id=correlation_id,
            )
        response.headers[CORRELATION_HEADER] = correlation_id
        if running.closing:
            logger.warning("incident_rejected_shutting_down", correlation_id=correlation_id)
            raise HTTPException(
                status_code=503,
                detail=SHUTTING_DOWN_DETAIL,
                headers={"Retry-After": str(RETRY_AFTER_S)},
            )

        # ── Deduplication: cache, then the stored incidents, then the cache again ──
        existing = dedup.check(correlation_id)
        if existing is not None:
            return _duplicate(correlation_id, existing, "memory")
        stored = await _stored_original(correlation_id)
        if stored is not None:
            dedup.record(correlation_id, stored)
            return _duplicate(correlation_id, stored, "db")
        # The lookup awaited: a concurrent request may have recorded the id meanwhile.
        existing = dedup.check(correlation_id)
        if existing is not None:
            return _duplicate(correlation_id, existing, "memory")

        # ── New incident (no await from the re-check to the record) ────────────
        if pipeline_fn is not None and running.at_capacity:
            logger.warning(
                "incident_rejected_at_capacity",
                correlation_id=correlation_id,
                in_flight=len(running),
                max_concurrent=running.max_concurrent,
            )
            raise HTTPException(
                status_code=429,
                detail=f"too many incidents in flight (max {running.max_concurrent}); retry later",
                headers={"Retry-After": str(RETRY_AFTER_S)},
            )
        incident_id = str(uuid.uuid4())
        request = PipelineInput.from_payload(incident_id, payload)
        dedup.record(correlation_id, incident_id)
        short_term_memory.create(incident_id, request.alert)

        logger.info(
            "incident_received",
            incident_id=incident_id,
            correlation_id=correlation_id,
            signal_type=payload.signal_type,
            service=request.alert.service,
            severity=request.alert.severity,
        )

        if pipeline_fn is not None:
            running.start(incident_id, _await(pipeline_fn(request)))
        else:
            logger.warning("pipeline_not_wired", incident_id=incident_id)

        return IncidentAck(
            incident_id=incident_id,
            correlation_id=correlation_id,
            status="accepted",
            message=ACCEPTED_MESSAGE,
        )

    return router
