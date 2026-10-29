"""Agent checkpoints — the ``incident_checkpoints`` table (decision 2026-10-25 B).

An incident used to live only in short-term memory until its terminal row was stored, so a
pod restart lost it. Now the pipeline inserts a placeholder ``incidents`` row (status
:data:`INCIDENT_IN_PROGRESS`, no embedding, no trajectory) when it starts, and writes one
checkpoint per stage: the full :class:`~sentinel.models.checkpoint.RunSnapshot`. A restarted
pod resumes each stuck incident once from its latest checkpoint
(:mod:`sentinel.infra.incident_resume`); the checkpoints are deleted once the terminal
incident is stored. They never leave the database (R14: they hold raw tool outputs).

:class:`CheckpointStore` is the SQL. :class:`CheckpointWriter` keeps every write off the
pipeline's critical path: one ordered chain of background tasks per incident (placeholder →
checkpoint 0 → … → delete), each bounded by :data:`CHECKPOINT_WRITE_TIMEOUT_S`, never
raising (``checkpoint_write_failed``), drained at shutdown with the dataset writes.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Final

# asyncpg ships no type stubs (see infra/database.py); only its types are named here.
import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from pydantic import JsonValue

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra import database
from sentinel.infra.logging import get_logger
from sentinel.models.checkpoint import Checkpoint, CheckpointStage, RunSnapshot
from sentinel.models.pipeline import PipelineInput

__all__ = [
    "CHECKPOINT_WRITE_TIMEOUT_S",
    "INCIDENT_IN_PROGRESS",
    "RESUME_FAILED",
    "CheckpointStore",
    "CheckpointWriter",
]

logger = get_logger("sentinel.memory.checkpoints")

# The status of an incident's placeholder row while its pipeline runs.
INCIDENT_IN_PROGRESS: Final = "in_progress"
# The escalation reason of an incident whose one resume after a restart did not finish.
RESUME_FAILED: Final = "resumed after restart failed"
# One checkpoint write (or the placeholder insert, or the delete), in seconds.
CHECKPOINT_WRITE_TIMEOUT_S: Final = 10.0
# After the drain's grace expires and the stragglers are cancelled: how long they may unwind.
_CANCEL_WAIT_S: Final = 0.5

_NO_POOL = "memory unavailable: no database configured (DATABASE_URL is not set)"
_RESUMED: Final = "resumed"


class CheckpointStore:
    """The SQL of ``incident_checkpoints`` and of the placeholder ``incidents`` row.

    With no pool every method raises :class:`SentinelMemoryError`, as does a database error.
    """

    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self._pool = pool

    async def open_incident(self, request: PipelineInput, started_at: datetime) -> None:
        """Insert the incident's ``in_progress`` placeholder row; a no-op when it exists."""
        alert = request.alert
        await self._execute(
            "INSERT INTO incidents (id, alert_id, dd_event_id, correlation_id, severity, "
            "service, status, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "ON CONFLICT (id) DO NOTHING",
            _uuid(request.incident_id),
            alert.alert_id,
            alert.dd_event_id,
            _uuid(request.correlation_id),
            alert.severity,
            alert.service,
            INCIDENT_IN_PROGRESS,
            started_at,
        )

    async def write(
        self,
        incident_id: str,
        step: int,
        stage: CheckpointStage,
        state: Mapping[str, JsonValue],
    ) -> None:
        """Write checkpoint *step*; a step already written is kept as it is."""
        await self._execute(
            "INSERT INTO incident_checkpoints (incident_id, step, stage, state) "
            "VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (incident_id, step) DO NOTHING",
            _uuid(incident_id),
            step,
            stage,
            json.dumps(dict(state)),
        )

    async def latest(self, incident_id: str) -> Checkpoint | None:
        """The highest-step checkpoint that is a snapshot (not a ``resumed`` marker)."""
        row = await self._fetchrow(
            "SELECT incident_id, step, stage, state, created_at FROM incident_checkpoints "
            "WHERE incident_id = $1 AND stage <> $2 ORDER BY step DESC LIMIT 1",
            _uuid(incident_id),
            _RESUMED,
        )
        if row is None:
            return None
        state: JsonValue = json.loads(row["state"])
        return Checkpoint(
            incident_id=str(row["incident_id"]),
            step=row["step"],
            stage=row["stage"],
            state=state if isinstance(state, dict) else {},
            created_at=row["created_at"],
        )

    async def last_step(self, incident_id: str) -> int | None:
        """The highest step written for the incident, markers included; None for none."""
        row = await self._fetchrow(
            "SELECT max(step) AS step FROM incident_checkpoints WHERE incident_id = $1",
            _uuid(incident_id),
        )
        return None if row is None else row["step"]

    async def was_resumed(self, incident_id: str) -> bool:
        """True once a resume of the incident was started (its ``resumed`` marker exists)."""
        row = await self._fetchrow(
            "SELECT EXISTS (SELECT 1 FROM incident_checkpoints "
            "WHERE incident_id = $1 AND stage = $2) AS resumed",
            _uuid(incident_id),
            _RESUMED,
        )
        return row is not None and bool(row["resumed"])

    async def stuck_incidents(self) -> list[str]:
        """Every ``in_progress`` incident, oldest first."""
        rows = await self._fetch(
            "SELECT id FROM incidents WHERE status = $1 ORDER BY created_at", INCIDENT_IN_PROGRESS
        )
        return [str(row["id"]) for row in rows]

    async def is_in_progress(self, incident_id: str) -> bool:
        """True when the incident's row is an ``in_progress`` placeholder."""
        row = await self._fetchrow("SELECT status FROM incidents WHERE id = $1", _uuid(incident_id))
        return row is not None and row["status"] == INCIDENT_IN_PROGRESS

    async def delete(self, incident_id: str) -> None:
        """Delete every checkpoint of the incident."""
        await self._execute(
            "DELETE FROM incident_checkpoints WHERE incident_id = $1", _uuid(incident_id)
        )

    async def mark_abandoned(self, incident_id: str, reason: str) -> None:
        """Close a placeholder that has no checkpoint to resume from as ``escalated``."""
        await self._execute(
            "UPDATE incidents SET status = 'escalated', resolution_type = 'escalated', "
            "resolution = $2, resolved_at = now() WHERE id = $1 AND status = $3",
            _uuid(incident_id),
            reason,
            INCIDENT_IN_PROGRESS,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise SentinelMemoryError(_NO_POOL)
        return self._pool

    async def _execute(self, sql: str, *args: object) -> None:
        pool = self._require_pool()
        try:
            async with database.acquire(pool) as conn:
                await conn.execute(sql, *args)
        except (asyncpg.PostgresError, OSError) as exc:
            raise SentinelMemoryError(f"checkpoint operation failed: {exc}") from exc

    async def _fetchrow(self, sql: str, *args: object) -> asyncpg.Record | None:
        pool = self._require_pool()
        try:
            async with database.acquire(pool) as conn:
                return await conn.fetchrow(sql, *args)
        except (asyncpg.PostgresError, OSError) as exc:
            raise SentinelMemoryError(f"checkpoint operation failed: {exc}") from exc

    async def _fetch(self, sql: str, *args: object) -> list[asyncpg.Record]:
        pool = self._require_pool()
        try:
            async with database.acquire(pool) as conn:
                return await conn.fetch(sql, *args)
        except (asyncpg.PostgresError, OSError) as exc:
            raise SentinelMemoryError(f"checkpoint operation failed: {exc}") from exc


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise SentinelMemoryError(f"{value!r} is not a UUID") from exc


class CheckpointWriter:
    """Background, ordered, bounded checkpoint writes — never on the pipeline's path.

    Every call returns at once. The writes of one incident run in call order, one at a
    time; each is bounded by *timeout_s* and a failure is logged, never raised. A failed
    placeholder insert disables the incident's later writes (its checkpoints would have no
    row to reference).
    """

    def __init__(
        self, store: CheckpointStore, *, timeout_s: float = CHECKPOINT_WRITE_TIMEOUT_S
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._store = store
        self._timeout_s = timeout_s
        self._tails: dict[str, asyncio.Task[None]] = {}
        self._disabled: set[str] = set()
        self._tasks: dict[asyncio.Task[None], str] = {}

    def __len__(self) -> int:
        return len(self._tails)

    def open(self, request: PipelineInput, started_at: datetime) -> None:
        """Insert the incident's placeholder row in the background."""
        incident_id = request.incident_id

        def disable(exc: BaseException) -> None:
            self._disabled.add(incident_id)
            logger.warning(
                "checkpoints_disabled_for_incident",
                incident_id=incident_id,
                error_type=type(exc).__name__,
            )

        self._chain(
            incident_id,
            "open",
            lambda: self._store.open_incident(request, started_at),
            on_failure=disable,
        )

    def save(self, incident_id: str, step: int, snapshot: RunSnapshot) -> None:
        """Write checkpoint *step* of *snapshot* in the background (serialised now)."""
        if incident_id in self._disabled:
            return
        state: dict[str, JsonValue] = snapshot.model_dump(mode="json")
        stage = snapshot.stage
        self._chain(
            incident_id, "write", lambda: self._store.write(incident_id, step, stage, state)
        )

    def discard(self, incident_id: str) -> None:
        """Delete the incident's checkpoints in the background, after its pending writes."""

        async def delete() -> None:
            try:
                await self._store.delete(incident_id)
            finally:
                self._disabled.discard(incident_id)

        self._chain(incident_id, "delete", delete, always=True)

    async def flush(self, incident_id: str) -> None:
        """Wait until every write of *incident_id* queued so far has run."""
        tail = self._tails.get(incident_id)
        if tail is not None:
            await asyncio.wait([tail])

    async def drain(self, grace_s: float) -> list[str]:
        """Wait up to *grace_s* for the writes, then cancel the rest; their incident ids."""
        snapshot = dict(self._tasks)
        if not snapshot:
            return []
        _, pending = await asyncio.wait(snapshot, timeout=max(grace_s, 0.0))
        for task in pending:
            logger.warning("checkpoint_write_cancelled_on_shutdown", incident_id=snapshot[task])
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=_CANCEL_WAIT_S)
        return sorted({snapshot[task] for task in pending})

    def _chain(
        self,
        incident_id: str,
        op_name: str,
        factory: Callable[[], Awaitable[None]],
        *,
        on_failure: Callable[[BaseException], None] | None = None,
        always: bool = False,
    ) -> None:
        """Run ``factory()`` after the incident's previous write, bounded and logged."""
        previous = self._tails.get(incident_id)

        async def run() -> None:
            if previous is not None:
                await asyncio.wait([previous])  # never raises, whatever previous did
            if incident_id in self._disabled and not always:
                return
            try:
                async with asyncio.timeout(self._timeout_s):
                    await factory()
            except Exception as exc:
                logger.warning(
                    "checkpoint_write_failed",
                    incident_id=incident_id,
                    op=op_name,
                    error_type=type(exc).__name__,
                )
                if on_failure is not None:
                    on_failure(exc)

        task = asyncio.create_task(run(), name=f"checkpoint-{incident_id}")
        self._tails[incident_id] = task
        self._tasks[task] = incident_id
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        incident_id = self._tasks.pop(task, None)
        if incident_id is not None and self._tails.get(incident_id) is task:
            del self._tails[incident_id]
