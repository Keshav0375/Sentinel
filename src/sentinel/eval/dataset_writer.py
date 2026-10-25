"""Terminal-state dataset writer — one LangFuse dataset item per finished incident.

The eval runner (``sentinel.eval.runner``) scores the items of the ``sentinel-scenario-runs``
dataset; this module writes them. When an incident reaches ``rollback_proposed`` or
``escalated`` the orchestrator hands the writer a :class:`TerminalRun`, and the writer
creates one item whose ``input`` is the runner's own ``ScenarioRun`` schema. Rules:

- **Branch label (D1).** ``alert.context.pr_details.branch`` (the PR's ``head.ref``, forwarded
  by ``scripts/incident_dispatch.py``). It must pass ``BRANCH_PATTERN`` (the payload validator
  drops one that does not) and be a scenario catalog branch, else no item and a
  ``scenario_run_item_skipped`` warning with a reason (the value is never logged).
- **PR and sha (D2).** ``pr_number`` is ``pr_details.number``; ``merge_sha`` is the stripped
  ``pr_details.sha``, or None when it is not 7-40 hex.
- **One incident per item (D3).** ``incidents`` holds exactly the one terminal incident.
- **Correlated deploy (D4).** The verified rollback target; else the last critique's suspect
  deploy when it names a PR; else None. The alert's ``tags.version`` is never used.
- **R14, narrow exception (D5).** The LLM-written root cause, evidence summary, justification,
  hypothesis, evidence and missing items are exported. Raw clips of tool output are not: the
  escalation's fallback evidence (``tool: output`` clips) is emptied, and an unparsed analysis
  reply used as the hypothesis (and the root cause) is replaced by :data:`WITHHELD_REPLY`, on
  every escalation, the resolution-drafted one included.
- **Trajectory (D6).** The orchestrator's status-only timeline (ok / error:kind), never a
  tool payload and never a span export.
- **Client (D7).** The LangFuse v3 SDK on a daemon thread, started in the background by the
  orchestrator (:class:`DatasetWrites`, hard ``DATASET_WRITE_TIMEOUT_S``, drained within the
  shutdown grace). The item id is the incident UUID, so a re-write is idempotent. Best effort:
  a failure is logged (type and field names only), never raised.
- **Gating (D8).** :func:`default_writer` is None on the fake LLM or without LangFuse keys.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache, partial
from typing import Any, Final, Protocol, cast

from langfuse.api.core.api_error import ApiError
from pydantic import ValidationError

from sentinel.catalog import catalog_branches
from sentinel.config import Settings
from sentinel.eval.runner import DATASET_NAME, IncidentRecord, ScenarioRun
from sentinel.infra.logging import get_logger
from sentinel.infra.tracing import get_client, incident_trace_id, langfuse_enabled
from sentinel.models.incident import TimelineEntry
from sentinel.models.pipeline import PipelineInput, PipelineResult
from sentinel.models.reflexion import SuspectDeploy

__all__ = [
    "DATASET_DESCRIPTION",
    "DATASET_WRITES",
    "DATASET_WRITE_TIMEOUT_S",
    "SKIP_NOT_IN_CATALOG",
    "SKIP_NO_BRANCH",
    "WITHHELD_REPLY",
    "DatasetClient",
    "DatasetWrites",
    "LangfuseScenarioWriter",
    "ScenarioRunWriter",
    "TerminalRun",
    "correlated_deploy",
    "default_writer",
    "export_result",
    "scenario_run_item",
    "skip_reason",
    "trajectory",
]

logger = get_logger("sentinel.eval.dataset_writer")

DATASET_DESCRIPTION: Final = (
    "Finished Sentinel incidents, one item per terminal incident (schema: sentinel/eval/runner.py)."
)
# Stands in for an unparsed analysis reply (raw model text is not exported).
WITHHELD_REPLY: Final = "[withheld: the analysis reply did not parse; raw replies are not exported]"
_SHA_RE: Final = re.compile(r"^[0-9a-fA-F]{7,40}$")
_DEPLOY_RE: Final = re.compile(r"^pr-\d+-[0-9a-fA-F]{7,40}$")
# Hard bound on one background write (decision 2026-10-25, amended after review).
DATASET_WRITE_TIMEOUT_S: Final = 10.0
# Unwinding a cancelled write: it only awaits a future.
_CANCEL_WAIT_S: Final = 0.5
SKIP_NO_BRANCH: Final = "no valid pr_details.branch"
SKIP_NOT_IN_CATALOG: Final = "pr_details.branch is not a scenario catalog branch"
_HTTP_CONFLICT: Final = 409
_FIELD_RE: Final = re.compile(r"[a-z_][a-z0-9_]{0,63}")
_THREAD_NAME: Final = "sentinel-dataset-item"


@dataclass(frozen=True)
class TerminalRun:
    """A finished incident, as the orchestrator hands it to the writer."""

    request: PipelineInput
    result: PipelineResult
    timeline: tuple[TimelineEntry, ...]
    # The last critique's suspect deploy.
    suspect: SuspectDeploy | None
    # escalation.hypothesis is the unparsed-analysis clip.
    raw_hypothesis: bool
    # escalation.evidence is _escalate's tool-output fallback.
    raw_evidence: bool


class DatasetClient(Protocol):
    """The slice of the LangFuse v3 client the writer calls."""

    def create_dataset(self, *, name: str, description: str | None = None) -> Any: ...

    def create_dataset_item(
        self,
        *,
        dataset_name: str,
        input: Any = None,  # noqa: A002 - the SDK's parameter name
        metadata: Any = None,
        source_trace_id: str | None = None,
        id: str | None = None,  # noqa: A002 - the SDK's parameter name
    ) -> Any: ...


class ScenarioRunWriter(Protocol):
    """Writes a terminal incident's dataset item; True when an item was created."""

    async def write(self, run: TerminalRun) -> bool: ...


def export_result(run: TerminalRun) -> PipelineResult:
    """*run*'s result with raw tool-output clips removed (D5); a rollback is unchanged."""
    escalation = run.result.escalation
    if escalation is None or not (run.raw_evidence or run.raw_hypothesis):
        return run.result
    update: dict[str, Any] = {}
    if run.raw_evidence:
        update["evidence"] = []
    if run.raw_hypothesis:
        update["hypothesis"] = WITHHELD_REPLY
    copied = run.result.model_copy(
        update={
            "escalation": escalation.model_copy(update=update),
            "root_cause": WITHHELD_REPLY if run.raw_hypothesis else run.result.root_cause,
        }
    )
    return PipelineResult.model_validate(copied.model_dump())


def correlated_deploy(run: TerminalRun) -> str | None:
    """The deploy the incident was correlated to, ``pr-<N>-<sha>`` (D4); None when unknown."""
    spec = run.result.rollback_spec
    found: str | None = None
    if spec is not None:
        target = spec.target_deploy
        found = f"pr-{target.pr_number}-{target.commit_sha}"
    elif run.suspect is not None and run.suspect.pr_number is not None:
        found = f"pr-{run.suspect.pr_number}-{run.suspect.commit_sha}"
    return found if found is not None and _DEPLOY_RE.match(found) else None


def trajectory(run: TerminalRun) -> dict[str, Any]:
    """The incident's status-only trajectory (D6): ``stm_context`` with the timeline."""
    return {
        "incident_id": run.result.incident_id,
        "stm_context": {
            "status": run.result.status,
            "timeline": [entry.model_dump(mode="json") for entry in run.timeline],
        },
    }


def skip_reason(run: TerminalRun) -> str | None:
    """Why *run* gets no dataset item (D1), or None when its branch label is usable."""
    details = run.request.alert.context.pr_details
    if details is None or details.branch is None:
        return SKIP_NO_BRANCH
    if details.branch not in catalog_branches():
        return SKIP_NOT_IN_CATALOG
    return None


def scenario_run_item(run: TerminalRun) -> dict[str, Any] | None:
    """The dataset item's ``input`` (a ``ScenarioRun`` dump); None when it is skipped (D1)."""
    if skip_reason(run) is not None:
        return None
    details = run.request.alert.context.pr_details
    assert details is not None and details.branch is not None  # skip_reason checked both
    branch = details.branch
    sha = run.request.alert.pr_sha
    scenario = ScenarioRun(
        branch=branch,
        pr_number=details.number,
        merge_sha=sha if sha is not None and _SHA_RE.match(sha) else None,
        incidents=[
            IncidentRecord(
                incident_id=run.result.incident_id,
                signal_type=run.request.signal_type,
                correlated_deploy=correlated_deploy(run),
                pipeline_result=export_result(run),
                trajectory=trajectory(run),
            )
        ],
    )
    return scenario.model_dump(mode="json")


class LangfuseScenarioWriter:
    """Writes the item to the ``sentinel-scenario-runs`` dataset; never raises."""

    def __init__(
        self, client: DatasetClient | None = None, *, dataset_name: str = DATASET_NAME
    ) -> None:
        self._client = client
        self._dataset_name = dataset_name
        self._dataset_ready = False

    async def write(self, run: TerminalRun) -> bool:
        """Create the incident's item; True only when the item call returned."""
        incident_id = run.result.incident_id
        reason = skip_reason(run)
        if reason is not None:
            logger.warning("scenario_run_item_skipped", incident_id=incident_id, reason=reason)
            return False
        try:
            item = scenario_run_item(run)
        except ValidationError as exc:
            self._failed(incident_id, exc)
            return False
        if item is None:  # unreachable after skip_reason; narrows the type
            return False
        try:
            await _in_daemon_thread(
                partial(self._create, incident_id, run.request.correlation_id, item)
            )
        except Exception as exc:
            self._failed(incident_id, exc)
            return False
        logger.info(
            "scenario_run_item_written",
            incident_id=incident_id,
            dataset=self._dataset_name,
            branch=item["branch"],
        )
        return True

    def _failed(self, incident_id: str, exc: Exception) -> None:
        """Log the failure's type (and a ValidationError's field names), never its message."""
        extra: dict[str, Any] = {}
        if isinstance(exc, ValidationError):
            extra["fields"] = sorted({_field_path(err["loc"]) for err in exc.errors()})
        logger.warning(
            "scenario_run_item_failed",
            incident_id=incident_id,
            error_type=type(exc).__name__,
            **extra,
        )

    def _create(self, incident_id: str, correlation_id: str, item: dict[str, Any]) -> None:
        """The thread body: the dataset once, then the item."""
        client = self._client or cast("DatasetClient", get_client())
        if not self._dataset_ready:
            try:
                client.create_dataset(name=self._dataset_name, description=DATASET_DESCRIPTION)
                self._dataset_ready = True
            except Exception as exc:
                # Ready only when it already exists (409); any other failure retries next write.
                conflict = isinstance(exc, ApiError) and exc.status_code == _HTTP_CONFLICT
                self._dataset_ready = conflict
                logger.info(
                    "scenario_dataset_create_skipped"
                    if conflict
                    else "scenario_dataset_create_failed",
                    dataset=self._dataset_name,
                    error_type=type(exc).__name__,
                )
        client.create_dataset_item(
            dataset_name=self._dataset_name,
            input=item,
            id=incident_id,
            source_trace_id=incident_trace_id(incident_id),
            metadata={"correlation_id": correlation_id},
        )


def _field_path(loc: tuple[int | str, ...]) -> str:
    """A ValidationError location with every segment that is not a field-name shape as ``*``."""
    return ".".join(
        part if isinstance(part, str) and _FIELD_RE.fullmatch(part) else "*" for part in loc
    )


async def _in_daemon_thread(fn: Callable[[], None]) -> None:
    """Run blocking *fn* on a daemon thread and await it (see D7 in the module doc)."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()

    def settle(error: Exception | None) -> None:
        if done.done():
            return  # the waiting task was cancelled (timeout or shutdown)
        if error is None:
            done.set_result(None)
        else:
            done.set_exception(error)

    def body() -> None:
        error: Exception | None = None
        try:
            fn()
        except Exception as exc:  # handed to the awaiting task
            error = exc
        try:
            loop.call_soon_threadsafe(settle, error)
        except RuntimeError:
            pass  # the loop is closed: nobody waits any more

    threading.Thread(target=body, name=_THREAD_NAME, daemon=True).start()
    await done


class DatasetWrites:
    """In-flight background dataset writes: each bounded by *timeout_s*, drained at shutdown."""

    def __init__(self, timeout_s: float = DATASET_WRITE_TIMEOUT_S) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._timeout_s = timeout_s
        self._tasks: dict[asyncio.Task[None], str] = {}

    def __len__(self) -> int:
        return len(self._tasks)

    def start(self, writer: ScenarioRunWriter, run: TerminalRun) -> asyncio.Task[None]:
        """Start *writer*'s write of *run* in the background; the returned task never raises."""
        incident_id = run.result.incident_id
        task = asyncio.create_task(self._write(writer, run), name=f"dataset-item-{incident_id}")
        self._tasks[task] = incident_id
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.pop(task, None)

    async def _write(self, writer: ScenarioRunWriter, run: TerminalRun) -> None:
        incident_id = run.result.incident_id
        try:
            async with asyncio.timeout(self._timeout_s):
                await writer.write(run)
        except TimeoutError:
            logger.warning(
                "scenario_run_item_timeout", incident_id=incident_id, timeout_s=self._timeout_s
            )
        except Exception as exc:
            logger.warning(
                "scenario_run_item_failed", incident_id=incident_id, error_type=type(exc).__name__
            )

    async def drain(self, grace_s: float) -> list[str]:
        """Wait up to *grace_s* for the writes, then cancel the rest; their incident ids."""
        snapshot = dict(self._tasks)
        if not snapshot:
            return []
        _, pending = await asyncio.wait(snapshot, timeout=max(grace_s, 0.0))
        for task in pending:
            logger.warning("scenario_run_item_cancelled_on_shutdown", incident_id=snapshot[task])
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=_CANCEL_WAIT_S)
        return sorted(snapshot[task] for task in pending)


# The process's writes; ``main.lifespan`` drains it at shutdown.
DATASET_WRITES: Final = DatasetWrites()


@cache
def _langfuse_writer() -> LangfuseScenarioWriter:
    return LangfuseScenarioWriter()


def default_writer(settings: Settings) -> ScenarioRunWriter | None:
    """The process-wide LangFuse writer; None on the fake LLM or without LangFuse keys (D8)."""
    if settings.sentinel_fake_llm or not langfuse_enabled():
        return None
    return _langfuse_writer()
