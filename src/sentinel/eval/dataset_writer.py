"""Terminal-state dataset writer — one LangFuse dataset item per finished incident.

The eval runner (``sentinel.eval.runner``) scores the items of the ``sentinel-scenario-runs``
dataset; this module writes them. When an incident reaches ``rollback_proposed`` or
``escalated`` the orchestrator hands the writer a :class:`TerminalRun`, and the writer
creates one item whose ``input`` is the runner's own ``ScenarioRun`` schema. Rules:

- **Branch label (D1).** ``alert.context.pr_details.branch`` (the PR's ``head.ref``, forwarded
  by ``scripts/incident_dispatch.py``). No branch, or a blank one: no item, a
  ``scenario_run_item_skipped`` warning.
- **PR and sha (D2).** ``pr_number`` is ``pr_details.number``; ``merge_sha`` is the stripped
  ``pr_details.sha``, or None when it is not 7-40 hex.
- **One incident per item (D3).** ``incidents`` holds exactly the one terminal incident.
- **Correlated deploy (D4).** The verified rollback target; else the last critique's suspect
  deploy when it names a PR; else None. The alert's ``tags.version`` is never used.
- **R14, narrow exception (D5).** The LLM-written root cause, evidence summary, justification,
  hypothesis, evidence and missing items are exported. Raw clips of tool output are not: the
  escalation's fallback evidence (``tool: output`` clips) is emptied, and an unparsed analysis
  reply used as the hypothesis (and the root cause) is replaced by :data:`WITHHELD_REPLY`.
- **Trajectory (D6).** The orchestrator's status-only timeline (ok / error:kind), never a
  tool payload and never a span export.
- **Client (D7).** The LangFuse v3 SDK, in a worker thread (the calls block). The item id is
  the incident UUID, so a re-write is idempotent. Best effort: a failure is logged, never raised.
- **Gating (D8).** :func:`default_writer` is None on the fake LLM or without LangFuse keys.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from functools import cache
from typing import Any, Final, Protocol, cast

from pydantic import ValidationError

from sentinel.config import Settings
from sentinel.eval.runner import DATASET_NAME, IncidentRecord, ScenarioRun
from sentinel.infra.logging import get_logger
from sentinel.infra.tracing import get_client, incident_trace_id, langfuse_enabled
from sentinel.models.incident import TimelineEntry
from sentinel.models.pipeline import PipelineInput, PipelineResult
from sentinel.models.reflexion import SuspectDeploy

__all__ = [
    "DATASET_DESCRIPTION",
    "WITHHELD_REPLY",
    "DatasetClient",
    "LangfuseScenarioWriter",
    "ScenarioRunWriter",
    "TerminalRun",
    "correlated_deploy",
    "default_writer",
    "export_result",
    "scenario_run_item",
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


def scenario_run_item(run: TerminalRun) -> dict[str, Any] | None:
    """The dataset item's ``input`` (a ``ScenarioRun`` dump); None when there is no branch (D1)."""
    details = run.request.alert.context.pr_details
    branch = details.branch.strip() if details is not None and details.branch else ""
    if details is None or not branch:
        return None
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
        try:
            item = scenario_run_item(run)
        except ValidationError as exc:
            self._failed(incident_id, exc)
            return False
        if item is None:
            logger.warning(
                "scenario_run_item_skipped", incident_id=incident_id, reason="no pr_details.branch"
            )
            return False
        try:
            await asyncio.to_thread(self._create, incident_id, run.request.correlation_id, item)
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
        logger.warning(
            "scenario_run_item_failed",
            incident_id=incident_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    def _create(self, incident_id: str, correlation_id: str, item: dict[str, Any]) -> None:
        """The thread body: the dataset once, then the item."""
        client = self._client or cast("DatasetClient", get_client())
        if not self._dataset_ready:
            try:
                client.create_dataset(name=self._dataset_name, description=DATASET_DESCRIPTION)
            except Exception as exc:  # already exists, most likely
                logger.info(
                    "scenario_dataset_create_skipped",
                    dataset=self._dataset_name,
                    error_type=type(exc).__name__,
                )
            self._dataset_ready = True
        client.create_dataset_item(
            dataset_name=self._dataset_name,
            input=item,
            id=incident_id,
            source_trace_id=incident_trace_id(incident_id),
            metadata={"correlation_id": correlation_id},
        )


@cache
def _langfuse_writer() -> LangfuseScenarioWriter:
    return LangfuseScenarioWriter()


def default_writer(settings: Settings) -> ScenarioRunWriter | None:
    """The process-wide LangFuse writer; None on the fake LLM or without LangFuse keys (D8)."""
    if settings.sentinel_fake_llm or not langfuse_enabled():
        return None
    return _langfuse_writer()
