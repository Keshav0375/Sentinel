"""Bounded resume of incidents a restart interrupted (decision 2026-10-25 B).

An incident whose pod died mid-pipeline is left as an ``in_progress`` placeholder row with
its stage checkpoints (``memory/checkpoints``). :class:`IncidentResumer` finds them — once at
startup (:meth:`~IncidentResumer.sweep`, a background task that never delays ``/ready``) and
when the webhook sees a retry of one (:meth:`~IncidentResumer.resume_one`) — and gives each
exactly ONE resume:

- no checkpoint at all → the placeholder is closed as escalated (``mark_abandoned``);
- a ``resumed`` marker already written (a resume was started and did not finish) → the
  incident escalates with :data:`~sentinel.memory.checkpoints.RESUME_FAILED` from its latest
  snapshot (``escalate_resumed``: stored, dataset item, checkpoints dropped);
- otherwise a slot is RESERVED (``IncidentTasks.reserve``: the cap is taken atomically, so a
  webhook cannot fill it while the marker is written), the ``resumed`` marker is written
  FIRST (if that fails the incident is not resumed, so the bound holds), short-term memory is
  refilled (the Redis mirror's timeline, when there is one), ``resumed`` is published and the
  pipeline restarts from the latest checkpoint as an ordinary
  :class:`~sentinel.infra.incident_tasks.IncidentTasks` task in that slot (shutdown drain
  included).

Outcomes: ``resumed``, ``escalated``, ``abandoned``; ``skipped`` when there is nothing to do
(not an ``in_progress`` placeholder, or its task already runs here); ``deferred`` when it
could not be resumed NOW — over the cap, shutting down, an error, the marker not written,
another resume of it in progress — and must be tried again at the next startup or webhook
retry (the webhook does not remember a deferred retry, so its next retry asks again).

The deployment's strategy is ``Recreate``: no two pods sweep at once.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from sentinel.agents.orchestrator import PipelineDeps, ResumePoint, escalate_resumed
from sentinel.infra.incident_tasks import IncidentCapacityError, IncidentTasks
from sentinel.infra.logging import get_logger
from sentinel.infra.status_events import StatusPublisher
from sentinel.memory.checkpoints import RESUME_FAILED, CheckpointStore
from sentinel.memory.short_term import RedisStmMirror, ShortTermMemory
from sentinel.models.pipeline import PipelineInput

__all__ = ["IncidentResumer", "IncidentRunFn", "ResumeOutcome"]

logger = get_logger("sentinel.infra.incident_resume")

ResumeOutcome = Literal["resumed", "escalated", "abandoned", "skipped", "deferred"]


class IncidentRunFn(Protocol):
    """The app's incident callback: a new incident, or (with *resume*) a resumed one."""

    def __call__(
        self, request: PipelineInput, resume: ResumePoint | None = None
    ) -> Awaitable[None]: ...


async def _await(awaitable: Awaitable[None]) -> None:
    """Wrap an awaitable as the coroutine ``IncidentTasks.start`` takes."""
    await awaitable


def _close(awaitable: Awaitable[None]) -> None:
    """Close an awaitable that will never run (no "coroutine was never awaited")."""
    if inspect.iscoroutine(awaitable):
        awaitable.close()


class IncidentResumer:
    """Resumes each stuck ``in_progress`` incident at most once."""

    def __init__(
        self,
        store: CheckpointStore,
        tasks: IncidentTasks,
        run_fn: IncidentRunFn,
        deps_fn: Callable[[], PipelineDeps],
        stm: ShortTermMemory,
        *,
        stm_mirror: RedisStmMirror | None = None,
        publisher: StatusPublisher | None = None,
    ) -> None:
        self._store = store
        self._tasks = tasks
        self._run_fn = run_fn
        self._deps_fn = deps_fn
        self._stm = stm
        self._stm_mirror = stm_mirror
        self._publisher = publisher
        self._busy: set[str] = set()

    async def sweep(self) -> list[str]:
        """Resume every stuck incident; the ids resumed. Never raises."""
        try:
            stuck = await self._store.stuck_incidents()
        except Exception as exc:
            logger.warning("incident_resume_sweep_failed", error_type=type(exc).__name__)
            return []
        resumed: list[str] = []
        for incident_id in stuck:
            if await self.resume_one(incident_id) == "resumed":
                resumed.append(incident_id)
        logger.info("incident_resume_sweep_done", stuck=len(stuck), resumed=len(resumed))
        return resumed

    async def resume_one(self, incident_id: str) -> ResumeOutcome:
        """Resume, escalate or close one stuck incident (see the module docs). Never raises."""
        if incident_id in self._busy:
            return "deferred"  # another resume of it is deciding; this one cannot know how
        if self._tasks.has(incident_id):
            return "skipped"
        self._busy.add(incident_id)
        try:
            return await self._resume(incident_id)
        except Exception as exc:
            logger.warning(
                "incident_resume_skipped",
                incident_id=incident_id,
                reason="error",
                error_type=type(exc).__name__,
            )
            return "deferred"
        finally:
            self._busy.discard(incident_id)
            self._tasks.release(incident_id)  # a no-op once its task started

    async def _resume(self, incident_id: str) -> ResumeOutcome:
        store = self._store
        if not await store.is_in_progress(incident_id):
            return "skipped"
        latest = await store.latest(incident_id)
        if latest is None:
            await store.mark_abandoned(incident_id, f"escalated: {RESUME_FAILED} (no checkpoint)")
            logger.warning("incident_abandoned", incident_id=incident_id, reason="no checkpoint")
            return "abandoned"
        snapshot = latest.snapshot()
        last_step = await store.last_step(incident_id)
        last = latest.step if last_step is None else last_step
        if await store.was_resumed(incident_id):
            await escalate_resumed(
                ResumePoint(snapshot, last + 1), self._deps_fn(), publisher=self._publisher
            )
            return "escalated"
        try:
            # The slot before any await: from here to start() no webhook can take it.
            self._tasks.reserve(incident_id)
        except IncidentCapacityError:
            return self._deferred(incident_id, "at capacity")
        except RuntimeError:
            return self._deferred(incident_id, "shutting down")
        try:
            # The marker first: whatever happens next, this incident is never resumed again.
            await store.write(incident_id, last + 1, "resumed", {"from_step": latest.step})
        except Exception as exc:
            logger.warning(
                "incident_resume_skipped",
                incident_id=incident_id,
                reason="the resumed marker could not be written",
                error_type=type(exc).__name__,
            )
            return "deferred"
        # Read the mirror before create(): create() writes the fresh state through to it.
        timeline = (
            await self._stm_mirror.load_timeline(incident_id)
            if self._stm_mirror is not None
            else []
        )
        self._stm.create(incident_id, snapshot.request.alert)
        for entry in timeline:
            self._stm.add_timeline_entry(incident_id, entry)
        if self._publisher is not None:
            self._publisher.publish(incident_id, "resumed")
        run = self._run_fn(snapshot.request, ResumePoint(snapshot, last + 2))
        try:
            self._tasks.start(incident_id, _await(run))  # the reserved slot
        except RuntimeError:  # shutdown began meanwhile; the marker bounds the next try
            _close(run)
            return self._deferred(incident_id, "shutting down")
        logger.info("incident_resumed", incident_id=incident_id, from_stage=snapshot.stage)
        return "resumed"

    @staticmethod
    def _deferred(incident_id: str, reason: str) -> ResumeOutcome:
        logger.warning("incident_resume_skipped", incident_id=incident_id, reason=reason)
        return "deferred"
