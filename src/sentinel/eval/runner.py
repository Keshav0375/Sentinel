"""Eval runner — a read-only scorer of finished scenario runs held in a LangFuse dataset.

The runner never runs the pipeline and never acts on the world (HITL): it does not merge,
deploy or revert, and it never calls GitHub. A human merges a scenario branch's PR, GitHub
Actions deploys it, Sentinel handles whatever fires; this module only scores what happened.
It reads the dataset, scores each item against ``branches.yaml``, pushes LangFuse scores
and writes the eval report. It writes nothing to the database (``incidents.eval_score`` is
set by ``sentinel.eval.judge.score_incident``).

Dataset: ``sentinel-scenario-runs`` (``DATASET_NAME``), read with the LangFuse v3 SDK. An
item's ``input`` is one finished scenario run; ``expected_output`` and ``metadata`` are
ignored (ground truth comes only from ``branches.yaml``)::

    {"branch": str, "pr_number": int|null, "merge_sha": str|null (7-40 hex),
     "incidents": [ {"incident_id": str,
                     "signal_type": "deploy_failure"|"runtime_error",
                     "correlated_deploy": "pr-<N>-<sha>"|null,
                     "pipeline_result": PipelineResult.model_dump(mode="json")|null,
                     "trajectory": {SentinelTracer JSON}} ]}

``incidents`` holds every incident Sentinel opened for that merge, in arrival order; ``[]``
means nothing fired. ``pipeline_result: null`` means the incident did not reach a terminal
state. ``correlated_deploy`` is the deploy the incident was correlated to.

Ground truth: the item's ``branch`` is joined to its ``branches.yaml`` entry (deployment
§4.1). ``expected_culprit: self`` binds to ``pr-<pr_number>-<merge_sha>``; an item that
needs it without both fields is invalid. Two deploys match when both parse as
``pr-<N>-<sha>``, the PR numbers are equal and one lower-cased sha is a prefix of the other.

Four checks per item, each 1.0 (pass) or 0.0 (fail):

- ``fired``: no signal expected -> no incident; otherwise at least one.
- ``classified``: no signal expected -> no incident; otherwise the expected signal type is
  among the incidents' types and every type is the expected one or in ``also_expected``.
- ``correlated``: no culprit -> no incident; otherwise at least one incident and every
  incident's ``correlated_deploy`` is the culprit deploy.
- ``revert_target``: on ``expected_resolution``. ``none``: no incident proposed a rollback.
  ``rollback``: one did, and every rollback targets the culprit. ``rollback_or_escalate``:
  there is an incident, each reached a result, and every rollback targets the culprit.

Judge: only the primary incident (the first with a pipeline result) is judged, by
``evaluate_trajectory`` against the branch's ground truth; no primary, no judge score. A
``judge_conflict`` item (the run used the judge's own model) is excluded from every
aggregate and pushes nothing.

Push: each item opens a LangFuse dataset run (``item.run``); the four checks go to its
trace as NUMERIC scores named after the check, next to the five judge dimensions. A push
failure is logged, never raised. The report is written to ``reports/eval_report.json`` and
``.md`` — the file ``GET /api/eval-report`` serves. Traces are flushed before returning.

Usage (library)::

    run = await run_eval(branches_path=Path("../Sentinel-deployment/scenarios/branches.yaml"))

Usage (CLI)::

    python3 scripts/run_eval.py --branches ../Sentinel-deployment/scenarios/branches.yaml \
        [--dataset N] [--run-name R]
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sentinel.config import Settings, get_settings
from sentinel.eval.judge import JudgeClient, evaluate_trajectory, make_judge_client, push_scores
from sentinel.eval.report import EvalReport, build_report, write_reports
from sentinel.exceptions import EvalError
from sentinel.infra.logging import configure_logging, get_logger
from sentinel.infra.tracing import flush_traces, get_client, langfuse_enabled, push_score
from sentinel.models.eval_result import GroundTruthCheck, GroundTruthResult, TrajectoryScore
from sentinel.models.pipeline import PipelineResult, SignalType

logger = get_logger("sentinel.eval.runner")

DATASET_NAME: Final = "sentinel-scenario-runs"
CHECK_PASS: Final = 1.0
CHECK_FAIL: Final = 0.0
_VERSION_RE: Final = re.compile(r"^pr-(\d+)-([0-9a-fA-F]{7,40})$")


class BranchTruth(BaseModel):
    """One scenarios/branches.yaml entry (deployment §4.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch: str
    case: Literal["i", "ii", "iii"]
    fault: str
    expected_signal_type: Literal["none", "deploy_failure", "runtime_error"]
    expected_resolution: Literal["none", "rollback", "rollback_or_escalate"]
    expected_failed_stage: Literal["build", "deploy", "verify"] | None = None
    also_expected: tuple[SignalType, ...] = ()
    expected_culprit: Literal["self", "none"]


class IncidentRecord(BaseModel):
    """One incident Sentinel opened for a scenario run."""

    incident_id: str
    signal_type: SignalType
    correlated_deploy: str | None = None
    pipeline_result: PipelineResult | None = None
    trajectory: dict[str, Any] = {}


class ScenarioRun(BaseModel):
    """A dataset item's input: one finished scenario run."""

    branch: str
    pr_number: int | None = None
    merge_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{7,40}$")
    incidents: list[IncidentRecord] = []


class RunSpan(Protocol):
    """The trace of one dataset-run item."""

    @property
    def trace_id(self) -> str: ...


class DatasetItem(Protocol):
    """What the runner uses of a Langfuse v3 DatasetItemClient."""

    @property
    def id(self) -> str: ...

    @property
    def input(self) -> Any: ...

    def run(
        self,
        *,
        run_name: str,
        run_metadata: Any | None = None,
        run_description: str | None = None,
    ) -> AbstractContextManager[RunSpan]: ...


@dataclass(frozen=True)
class ItemResult:
    """One scored dataset item."""

    item_id: str
    branch: str
    ground_truth: GroundTruthResult
    judge_score: TrajectoryScore | None
    pushed: bool

    @property
    def judge_conflict(self) -> bool:
        return self.ground_truth.judge_conflict


@dataclass(frozen=True)
class EvalRun:
    """The outcome of one ``run_eval``: per-item results, the report and where it was written."""

    dataset_name: str
    run_name: str
    results: list[ItemResult]
    report: EvalReport
    json_path: Path
    md_path: Path
    invalid: dict[str, str] = field(default_factory=dict[str, str])


def load_branches(path: Path) -> dict[str, BranchTruth]:
    """Read the branch catalog (scenarios/branches.yaml) into ``{branch: BranchTruth}``."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))  # pyright: ignore[reportUnknownMemberType]
    except (OSError, yaml.YAMLError) as exc:
        raise EvalError(f"cannot read the branch catalog {path}: {exc}") from exc
    entries: Any = cast("dict[str, Any]", raw).get("branches") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise EvalError(f"{path}: no 'branches' list")
    catalog: dict[str, BranchTruth] = {}
    for entry in cast("list[Any]", entries):
        try:
            truth = BranchTruth.model_validate(entry)
        except ValidationError as exc:
            raise EvalError(f"{path}: invalid entry: {exc}") from exc
        if truth.branch in catalog:
            raise EvalError(f"{path}: duplicate branch {truth.branch!r}")
        catalog[truth.branch] = truth
    return catalog


def fetch_dataset_items(dataset_name: str) -> list[DatasetItem]:
    """The items of LangFuse dataset *dataset_name*."""
    if not langfuse_enabled():
        raise EvalError(
            "LangFuse is not configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY "
            "to read a dataset"
        )
    try:
        dataset = get_client().get_dataset(dataset_name)
    except Exception as exc:  # noqa: BLE001
        raise EvalError(f"cannot read LangFuse dataset {dataset_name!r}: {exc}") from exc
    return cast("list[DatasetItem]", list(dataset.items))


def bind_culprit(truth: BranchTruth, run: ScenarioRun) -> str | None:
    """The deploy the branch's fault ships as (``pr-<N>-<sha>``), or None when it has none."""
    if truth.expected_culprit == "none":
        return None
    if run.pr_number is None or run.merge_sha is None:
        raise EvalError(
            f"{truth.branch}: expected_culprit is self but the run has no pr_number/merge_sha"
        )
    return f"pr-{run.pr_number}-{run.merge_sha}"


def same_deploy(version: str | None, culprit: str) -> bool:
    """True when *version* names the *culprit* deploy: same PR, one sha a prefix of the other."""
    if version is None:
        return False
    a = _VERSION_RE.match(version)
    b = _VERSION_RE.match(culprit)
    if a is None or b is None or int(a.group(1)) != int(b.group(1)):
        return False
    sha_a, sha_b = a.group(2).lower(), b.group(2).lower()
    return sha_a.startswith(sha_b) or sha_b.startswith(sha_a)


def rollback_target(result: PipelineResult) -> str | None:
    """The deploy a rollback reverts (``pr-<N>-<sha>``), or None when there is no spec."""
    if result.rollback_spec is None:
        return None
    target = result.rollback_spec.target_deploy
    return f"pr-{target.pr_number}-{target.commit_sha}"


def _rollbacks(run: ScenarioRun) -> list[PipelineResult]:
    return [
        i.pipeline_result
        for i in run.incidents
        if i.pipeline_result is not None and i.pipeline_result.status == "rollback_proposed"
    ]


def _fired(truth: BranchTruth, run: ScenarioRun) -> bool:
    if truth.expected_signal_type == "none":
        return not run.incidents
    return bool(run.incidents)


def _classified(truth: BranchTruth, run: ScenarioRun) -> bool:
    if truth.expected_signal_type == "none":
        return not run.incidents
    types = {i.signal_type for i in run.incidents}
    allowed: set[str] = {truth.expected_signal_type, *truth.also_expected}
    return truth.expected_signal_type in types and types <= allowed


def _correlated(run: ScenarioRun, culprit: str | None) -> bool:
    if culprit is None:
        return not run.incidents
    return bool(run.incidents) and all(
        same_deploy(i.correlated_deploy, culprit) for i in run.incidents
    )


def _reverted_right(truth: BranchTruth, run: ScenarioRun, culprit: str | None) -> bool:
    rollbacks = _rollbacks(run)
    if truth.expected_resolution == "none":
        return not rollbacks
    targets_ok = all(
        culprit is not None and same_deploy(rollback_target(r), culprit) for r in rollbacks
    )
    if truth.expected_resolution == "rollback":
        return bool(rollbacks) and targets_ok
    return (
        bool(run.incidents)
        and all(i.pipeline_result is not None for i in run.incidents)
        and targets_ok
    )


def ground_truth_checks(
    truth: BranchTruth, run: ScenarioRun, culprit: str | None
) -> dict[GroundTruthCheck, float]:
    """The four checks of *run* against *truth*, each ``CHECK_PASS`` or ``CHECK_FAIL``."""
    outcomes = {
        GroundTruthCheck.FIRED: _fired(truth, run),
        GroundTruthCheck.CLASSIFIED: _classified(truth, run),
        GroundTruthCheck.CORRELATED: _correlated(run, culprit),
        GroundTruthCheck.REVERT_TARGET: _reverted_right(truth, run, culprit),
    }
    return {check: CHECK_PASS if ok else CHECK_FAIL for check, ok in outcomes.items()}


def default_run_name() -> str:
    """The dataset-run name when none is given."""
    return f"eval-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"


async def score_item(
    item: DatasetItem,
    catalog: Mapping[str, BranchTruth],
    *,
    judge_model: str,
    judge_client: JudgeClient,
) -> tuple[GroundTruthResult, TrajectoryScore | None]:
    """Score one dataset item: the four ground-truth checks and, if there is one, the judge.

    Raises:
        EvalError: The item's input is invalid, its branch is not in the catalog, or its
            culprit cannot be bound.
    """
    try:
        run = ScenarioRun.model_validate(item.input)
    except ValidationError as exc:
        raise EvalError(f"item {item.id}: invalid input: {exc}") from exc
    truth = catalog.get(run.branch)
    if truth is None:
        raise EvalError(f"item {item.id}: branch {run.branch!r} is not in branches.yaml")
    culprit = bind_culprit(truth, run)
    checks = ground_truth_checks(truth, run, culprit)

    score: TrajectoryScore | None = None
    primary = next((i for i in run.incidents if i.pipeline_result is not None), None)
    if primary is not None and primary.pipeline_result is not None:
        trajectory = {
            **primary.trajectory,
            "pipeline_result": primary.pipeline_result.model_dump(mode="json"),
        }
        ground_truth = {**truth.model_dump(mode="json"), "expected_culprit": culprit or "none"}
        score = await evaluate_trajectory(
            trajectory,
            ground_truth,
            incident_id=primary.incident_id,
            scenario_id=run.branch,
            client=judge_client,
            model=judge_model,
        )

    result = GroundTruthResult(
        item_id=item.id,
        branch=run.branch,
        case=truth.case,
        checks=checks,
        judge_conflict=bool(score and score.judge_conflict),
    )
    logger.info(
        "eval_item_scored",
        item_id=item.id,
        branch=run.branch,
        checks={str(c): v for c, v in checks.items()},
        judge_total=score.total_score if score else None,
        judge_conflict=result.judge_conflict,
    )
    return result, score


def push_item(
    item: DatasetItem,
    *,
    run_name: str,
    ground_truth: GroundTruthResult,
    judge_score: TrajectoryScore | None,
) -> bool:
    """Push one item's scores on its dataset-run trace; False when not pushed (never raises)."""
    if ground_truth.judge_conflict:
        logger.warning("eval_item_conflict_not_pushed", item_id=item.id, branch=ground_truth.branch)
        return False
    try:
        with item.run(run_name=run_name, run_metadata={"branch": ground_truth.branch}) as span:
            for check in GroundTruthCheck:
                push_score(span.trace_id, str(check), ground_truth.score_for(check))
            if judge_score is not None:
                push_scores(judge_score, span.trace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("eval_item_push_failed", item_id=item.id, error=str(exc))
        return False
    return True


async def run_eval(
    *,
    branches_path: Path,
    dataset_name: str = DATASET_NAME,
    run_name: str | None = None,
    items: Sequence[DatasetItem] | None = None,
    settings: Settings | None = None,
    reports_dir: Path = Path("reports"),
) -> EvalRun:
    """Score every item of the dataset against the branch catalog and write the report.

    Args:
        branches_path: The scenarios/branches.yaml ground-truth catalog.
        dataset_name: The LangFuse dataset to read (ignored for the items when *items* is given).
        run_name: The dataset-run name; defaults to a UTC timestamp.
        items: Items to score instead of fetching the dataset (tests, replays).
        settings: Defaults to ``get_settings()``.
        reports_dir: Where ``eval_report.json`` and ``.md`` are written.

    Raises:
        EvalError: The catalog or the dataset cannot be read.
        ProviderUnavailableError: The judge provider's credential is not set.
    """
    resolved = settings or get_settings()
    configure_logging(resolved.sentinel_log_level)
    name = run_name or default_run_name()
    logger.info("eval_run_start", dataset=dataset_name, run_name=name)
    results: list[ItemResult] = []
    invalid: dict[str, str] = {}
    items_read = 0
    try:
        catalog = load_branches(branches_path)
        judge_model, judge_client = make_judge_client(resolved)
        dataset_items = (
            list(items)
            if items is not None
            else await asyncio.to_thread(fetch_dataset_items, dataset_name)
        )
        items_read = len(dataset_items)
        for item in dataset_items:
            try:
                ground_truth, judge_score = await score_item(
                    item, catalog, judge_model=judge_model, judge_client=judge_client
                )
            except EvalError as exc:
                logger.warning("eval_item_invalid", item_id=item.id, error=str(exc))
                invalid[item.id] = str(exc)
                continue
            pushed = push_item(
                item, run_name=name, ground_truth=ground_truth, judge_score=judge_score
            )
            results.append(
                ItemResult(
                    item_id=item.id,
                    branch=ground_truth.branch,
                    ground_truth=ground_truth,
                    judge_score=judge_score,
                    pushed=pushed,
                )
            )
        report = build_report(
            [r.judge_score for r in results if r.judge_score is not None],
            ground_truth=[r.ground_truth for r in results],
            dataset_name=dataset_name,
            run_name=name,
        )
        json_path, md_path = write_reports(report, reports_dir=reports_dir)
    finally:
        flush_traces()
    logger.info(
        "eval_run_complete",
        items_read=items_read,
        items_scored=report.items_scored,
        invalid=len(invalid),
        judge_conflicts=report.judge_conflicts,
        pushed=sum(1 for r in results if r.pushed),
    )
    return EvalRun(
        dataset_name=dataset_name,
        run_name=name,
        results=results,
        report=report,
        json_path=json_path,
        md_path=md_path,
        invalid=invalid,
    )
