"""Webhook receiver — POST /webhooks/alert."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import APIRouter, BackgroundTasks

from sentinel.infra.logging import get_logger
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertAck, AlertPayload

logger = get_logger("sentinel.api.webhooks")

# Type alias for the async pipeline callback injected at app startup.
PipelineFn = Callable[[str, AlertPayload], Awaitable[None]]

_DEDUP_TTL_SECONDS: float = 60.0


@dataclass
class _CacheEntry:
    """Single entry in the dedup cache."""

    incident_id: str
    recorded_at: float  # time.monotonic() — avoids clock/timezone issues


class AlertDeduplicator:
    """Idempotency cache for incoming alerts.

    Prevents the same alert from spawning multiple parallel pipeline runs when
    a monitoring system delivers the same alert more than once within a short
    window (common with PagerDuty and Datadog retry behaviour).

    Cache entries expire after ``TTL_SECONDS`` (default 60). After expiry, the
    same ``alert_id`` is treated as a fresh alert.
    """

    TTL_SECONDS: float = _DEDUP_TTL_SECONDS

    def __init__(self) -> None:
        self._cache: dict[str, _CacheEntry] = {}

    def check(self, alert_id: str) -> str | None:
        """Return the stored incident_id if the alert is a duplicate within TTL.

        Evicts the cache entry on the spot if it has expired.

        Args:
            alert_id: Unique identifier from the incoming ``AlertPayload``.

        Returns:
            The incident_id assigned on first receipt, or ``None`` if the alert
            is new or its cache entry has expired.
        """
        entry = self._cache.get(alert_id)
        if entry is None:
            return None
        if time.monotonic() - entry.recorded_at >= self.TTL_SECONDS:
            del self._cache[alert_id]
            return None
        return entry.incident_id

    def record(self, alert_id: str, incident_id: str) -> None:
        """Store a new alert_id → incident_id mapping with the current timestamp.

        Args:
            alert_id: Unique ID from the monitoring system.
            incident_id: Sentinel incident ID assigned to this alert.
        """
        self._cache[alert_id] = _CacheEntry(
            incident_id=incident_id,
            recorded_at=time.monotonic(),
        )

    def evict_expired(self) -> int:
        """Remove all entries older than ``TTL_SECONDS``.

        Returns:
            Number of evicted entries.
        """
        now = time.monotonic()
        expired = [
            aid for aid, entry in self._cache.items() if now - entry.recorded_at >= self.TTL_SECONDS
        ]
        for aid in expired:
            del self._cache[aid]
        return len(expired)

    @property
    def size(self) -> int:
        """Current number of cache entries (including any not-yet-evicted expired ones)."""
        return len(self._cache)


def _new_incident_id() -> str:
    """Generate a unique incident ID in the same format as ``Incident.id``."""
    return f"inc-{uuid.uuid4().hex[:8]}"


def make_alert_router(
    deduplicator: AlertDeduplicator,
    short_term_memory: ShortTermMemory,
    pipeline_fn: PipelineFn | None = None,
) -> APIRouter:
    """Create the alert webhook router with deduplication and background pipeline.

    Args:
        deduplicator: ``AlertDeduplicator`` for idempotency checks.
        short_term_memory: ``ShortTermMemory`` to register new incidents.
        pipeline_fn: Optional async callback ``(incident_id, alert) → None``
            that runs the full orchestrator pipeline in the background. When
            ``None``, the endpoint logs a warning and skips the pipeline (useful
            for testing the API layer in isolation, or before 6.3 is wired).

    Returns:
        ``APIRouter`` with ``POST /webhooks/alert`` registered.
    """
    router = APIRouter()

    @router.post("/webhooks/alert", status_code=202, response_model=AlertAck)
    async def receive_alert(  # pyright: ignore[reportUnusedFunction]
        payload: AlertPayload,
        background_tasks: BackgroundTasks,
    ) -> AlertAck:
        """Accept an alert from a monitoring system.

        Assigns an incident_id, deduplicates within a 60-second window, creates
        an incident in short-term memory, and queues the orchestrator pipeline
        as a background task. Returns 202 Accepted immediately.
        """
        # ── Deduplication ──────────────────────────────────────────────────────
        existing_id = deduplicator.check(payload.alert_id)
        if existing_id is not None:
            logger.info(
                "alert_deduplicated",
                alert_id=payload.alert_id,
                incident_id=existing_id,
            )
            return AlertAck(alert_id=payload.alert_id, incident_id=existing_id)

        # ── New alert ──────────────────────────────────────────────────────────
        incident_id = _new_incident_id()
        deduplicator.record(payload.alert_id, incident_id)
        short_term_memory.create(incident_id, payload)

        logger.info(
            "alert_received",
            alert_id=payload.alert_id,
            incident_id=incident_id,
            service=payload.service,
            severity=str(payload.severity),
        )

        if pipeline_fn is not None:
            background_tasks.add_task(pipeline_fn, incident_id, payload)
        else:
            logger.warning(
                "pipeline_not_wired",
                incident_id=incident_id,
            )

        return AlertAck(alert_id=payload.alert_id, incident_id=incident_id)

    return router
