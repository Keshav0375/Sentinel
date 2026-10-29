"""Short-term memory — a per-incident dict, in-process, optionally mirrored to Redis.

Reads are always local and synchronous. With a mirror (decision 2026-10-25 D) every
mutation is also written through to Redis in the background — coalesced, latest wins per
incident, key ``sentinel:stm:<incident_id>`` with a TTL past the longest pipeline — so a
restarted pod can refill a resumed incident's timeline. Without one (no ``REDIS_HOST``) the
behaviour is exactly in-process.
"""

from __future__ import annotations

import asyncio
import json
from enum import Enum
from typing import Any, Final, Protocol, cast

from pydantic import BaseModel, JsonValue, ValidationError

from sentinel.infra.logging import get_logger
from sentinel.infra.redis_client import RedisHandle
from sentinel.models.alert import IncidentAlert
from sentinel.models.incident import IncidentStatus, TimelineEntry

logger = get_logger("sentinel.memory.short_term")

# The Redis key prefix of a mirrored incident.
STM_KEY_PREFIX: Final = "sentinel:stm:"
# One mirror write or read, in seconds.
_MIRROR_OP_TIMEOUT_S: Final = 2.0
_CANCEL_WAIT_S: Final = 0.5


class StmMirror(Protocol):
    """Where short-term memory writes each incident's state through to."""

    def save(self, incident_id: str, state: dict[str, Any]) -> None: ...

    def remove(self, incident_id: str) -> None: ...


class ShortTermMemory:
    """In-process, per-incident working memory.

    Stores the live state of every active incident as a plain Python dict
    keyed by incident_id. Created when an alert arrives; discarded after the
    incident resolves and the trajectory is handed to the eval system.

    All methods are synchronous — no I/O, no awaiting. The dict lives in the
    process and is shared across all agents in the same event loop. With a *mirror*, each
    mutation is also handed to it (write-through, in the background); reads stay local.

    Typical lifecycle::

        stm.create(inc_id, alert)
        stm.add_timeline_entry(inc_id, TimelineEntry(...))
        context = stm.get_context(inc_id)
        stm.update_context(inc_id, status=IncidentStatus.INVESTIGATING)
        trajectory = stm.get_context(inc_id)   # eval reads this
        stm.clear(inc_id)
    """

    def __init__(self, mirror: StmMirror | None = None) -> None:
        self._incidents: dict[str, dict[str, Any]] = {}
        self._mirror = mirror

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def create(self, incident_id: str, alert: IncidentAlert) -> None:
        """Initialise a new incident record.

        Sets the result to None and status to TRIAGE. Overwrites any existing record
        for the same incident_id — callers should not reuse IDs.

        Args:
            incident_id: Unique identifier (a UUID string, as ``Incident.id``).
            alert: The incoming alert that triggered this incident.
        """
        self._incidents[incident_id] = {
            "incident_id": incident_id,
            "alert": alert,
            "timeline": [],
            # The PipelineResult (as JSON) once the pipeline has finished.
            "result": None,
            "status": IncidentStatus.TRIAGE,
        }
        self._mirror_save(incident_id)

    def clear(self, incident_id: str) -> None:
        """Remove an incident from memory. No-op if the incident does not exist."""
        self._incidents.pop(incident_id, None)
        if self._mirror is not None:
            try:
                self._mirror.remove(incident_id)
            except Exception as exc:
                logger.warning(
                    "stm_mirror_failed", incident_id=incident_id, error_type=type(exc).__name__
                )

    # ── Timeline ──────────────────────────────────────────────────────────────

    def add_timeline_entry(self, incident_id: str, entry: TimelineEntry) -> None:
        """Append a step to the incident timeline.

        Called by every agent after each tool call or handoff so the eval
        system can reconstruct the full trajectory later.

        Args:
            incident_id: Must correspond to an incident created via create().
            entry: The step to append.

        Raises:
            KeyError: If no incident with incident_id exists.
        """
        self._require(incident_id)
        self._incidents[incident_id]["timeline"].append(entry)
        self._mirror_save(incident_id)

    def get_timeline(self, incident_id: str) -> list[TimelineEntry]:
        """Return a copy of all timeline entries for an incident.

        Returns a new list on every call so callers cannot accidentally mutate
        the stored timeline. The entries themselves are not copied.

        Args:
            incident_id: Must correspond to a known incident.

        Returns:
            Timeline entries in insertion order.

        Raises:
            KeyError: If no incident with incident_id exists.
        """
        self._require(incident_id)
        return list(self._incidents[incident_id]["timeline"])

    # ── Context ───────────────────────────────────────────────────────────────

    def get_context(self, incident_id: str) -> dict[str, Any]:
        """Return the full incident state dict for agent/eval use.

        Returns a shallow copy of the stored dict. Mutable values inside the
        dict (such as the timeline list or result dicts) are shared, not
        copied. Prefer update_context() to mutate result fields rather than
        editing the returned dict directly.

        Args:
            incident_id: Must correspond to a known incident.

        Returns:
            Dict with keys: incident_id, alert, timeline, result, status.

        Raises:
            KeyError: If no incident with incident_id exists.
        """
        self._require(incident_id)
        return dict(self._incidents[incident_id])

    def update_context(self, incident_id: str, **fields: Any) -> None:
        """Overwrite one or more fields in the incident context dict.

        Agents call this to record their results and advance the incident
        status as they complete their work::

            stm.update_context(inc_id, status=IncidentStatus.INVESTIGATING)
            stm.update_context(inc_id, status=IncidentStatus.ESCALATED, result=...)

        Args:
            incident_id: Must correspond to a known incident.
            **fields: Keyword arguments are merged into the incident dict.

        Raises:
            KeyError: If no incident with incident_id exists.
        """
        self._require(incident_id)
        self._incidents[incident_id].update(fields)
        self._mirror_save(incident_id)

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def active_incident_ids(self) -> list[str]:
        """Return a sorted list of all currently tracked incident IDs."""
        return sorted(self._incidents)

    @property
    def active_count(self) -> int:
        """Number of incidents currently held in memory."""
        return len(self._incidents)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _require(self, incident_id: str) -> None:
        if incident_id not in self._incidents:
            raise KeyError(f"No incident in short-term memory: {incident_id!r}")

    def _mirror_save(self, incident_id: str) -> None:
        """Hand the incident's state to the mirror; a mirror failure never breaks STM."""
        if self._mirror is None:
            return
        try:
            self._mirror.save(incident_id, _serialisable(self._incidents[incident_id]))
        except Exception as exc:
            logger.warning(
                "stm_mirror_failed", incident_id=incident_id, error_type=type(exc).__name__
            )


def _serialisable(value: Any) -> Any:
    """*value* as plain JSON: models dumped, enums as their value, anything else as text."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return _serialisable(value.value)
    if isinstance(value, dict):
        items = cast(dict[object, Any], value).items()
        return {str(k): _serialisable(v) for k, v in items}
    if isinstance(value, list | tuple):
        return [_serialisable(v) for v in cast(list[Any], value)]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


class RedisStmMirror:
    """Writes short-term memory through to Redis: coalesced, latest wins, per incident.

    Each incident has at most one background writer; while it writes, newer states replace
    the pending one, so Redis ends at the latest state with no backlog. A failure is logged
    (``stm_mirror_failed``), never raised.
    """

    def __init__(self, redis: RedisHandle, ttl_s: int) -> None:
        if ttl_s < 1:
            raise ValueError("ttl_s must be >= 1")
        self._redis = redis
        self._ttl_s = ttl_s
        # incident id → the JSON to write next (None = delete the key).
        self._pending: dict[str, str | None] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}

    def save(self, incident_id: str, state: dict[str, Any]) -> None:
        self._enqueue(incident_id, json.dumps(state))

    def remove(self, incident_id: str) -> None:
        self._enqueue(incident_id, None)

    async def load_timeline(self, incident_id: str) -> list[TimelineEntry]:
        """The mirrored timeline of *incident_id*; [] when missing, invalid or unreachable."""
        try:
            async with asyncio.timeout(_MIRROR_OP_TIMEOUT_S):
                raw = await (await self._redis.client()).get(_key(incident_id))
            if raw is None:
                return []
            state: JsonValue = json.loads(raw)
            entries = state.get("timeline") if isinstance(state, dict) else None
            if not isinstance(entries, list):
                return []
            return [TimelineEntry.model_validate(e) for e in entries]
        except (ValidationError, ValueError):
            return []
        except Exception as exc:
            logger.warning(
                "stm_mirror_failed", incident_id=incident_id, error_type=type(exc).__name__
            )
            return []

    async def drain(self, grace_s: float) -> None:
        """Wait up to *grace_s* for the pending writes, then cancel the rest."""
        workers = set(self._workers.values())
        if not workers:
            return
        _, pending = await asyncio.wait(workers, timeout=max(grace_s, 0.0))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=_CANCEL_WAIT_S)

    def _enqueue(self, incident_id: str, value: str | None) -> None:
        self._pending[incident_id] = value
        if incident_id not in self._workers:
            write = self._write(incident_id)
            try:
                task = asyncio.create_task(write, name=f"stm-mirror-{incident_id}")
            except RuntimeError:  # no running loop: nothing can be written
                write.close()
                self._pending.pop(incident_id, None)
                raise
            self._workers[incident_id] = task

    async def _write(self, incident_id: str) -> None:
        try:
            while incident_id in self._pending:
                value = self._pending.pop(incident_id)
                try:
                    async with asyncio.timeout(_MIRROR_OP_TIMEOUT_S):
                        client = await self._redis.client()
                        if value is None:
                            await client.delete(_key(incident_id))
                        else:
                            await client.set(_key(incident_id), value, ex=self._ttl_s)
                except Exception as exc:
                    logger.warning(
                        "stm_mirror_failed",
                        incident_id=incident_id,
                        error_type=type(exc).__name__,
                    )
        finally:
            self._workers.pop(incident_id, None)


def _key(incident_id: str) -> str:
    return f"{STM_KEY_PREFIX}{incident_id}"
