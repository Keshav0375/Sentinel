"""Tests for eval/dataset_writer.py — the terminal-state LangFuse dataset item.

Nothing here needs a database or a network: the client is a recording fake, and the
pipeline runs on the fake LLM with the rollback-target cross-check stubbed (as
tests/test_agents/test_orchestrator.py does).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from structlog.testing import capture_logs

from sentinel.agents import orchestrator
from sentinel.agents.orchestrator import PipelineDeps, run_pipeline
from sentinel.config import Settings
from sentinel.eval import dataset_writer
from sentinel.eval.dataset_writer import (
    WITHHELD_REPLY,
    LangfuseScenarioWriter,
    TerminalRun,
    correlated_deploy,
    default_writer,
    export_result,
    scenario_run_item,
    trajectory,
)
from sentinel.eval.runner import DATASET_NAME, ScenarioRun
from sentinel.infra.logging import get_logger
from sentinel.infra.tracing import incident_trace_id
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.alert import IncidentAlert, IncidentContext, PRDetails
from sentinel.models.analysis import AnalysisResult
from sentinel.models.deploy import CorrelatedDeploy
from sentinel.models.incident import Severity, TimelineEntry, TriageResult
from sentinel.models.pipeline import PipelineInput, PipelineResult, SignalType
from sentinel.models.reflexion import SuspectDeploy
from sentinel.models.tools import (
    DeployRecord,
    EscalationContext,
    RollbackSpec,
    TargetDeploy,
)
from sentinel.providers import (
    REFLEXION_AGENT,
    REVERIFICATION_AGENT,
    FakeScript,
    FakeToolCall,
    FakeTurn,
    fake_script,
    reflexion_turn,
    reverification_turn,
)
from sentinel.tools.deploy_checker import TargetDeployCheck

SERVICE = "sentinel-watchtower"
SHA = "a1b2c3d"
SHA40 = "a" * 40
PR = 31
DEPLOYED_AT = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
MARK = "RAW-TOOL-MARKER-7f3c"
REPLY_MARK = "RAW-REPLY-MARKER-91ab"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _request(
    signal_type: SignalType = "deploy_failure",
    *,
    branch: str | None = "deployfail/03",
    sha: str | None = SHA40,
    pr_details: bool = True,
) -> PipelineInput:
    context = IncidentContext(
        pr_details=PRDetails(number=PR, sha=sha, branch=branch) if pr_details else None
    )
    alert = IncidentAlert(
        title="deploy failed", severity="critical", service=SERVICE, context=context
    )
    return PipelineInput(
        incident_id=str(uuid.uuid4()),
        alert=alert,
        signal_type=signal_type,
        correlation_id=str(uuid.uuid4()),
    )


def _rollback_result(request: PipelineInput, sha: str = SHA40) -> PipelineResult:
    spec = RollbackSpec(
        incident_id=request.incident_id,
        root_cause="pr-31 broke GET /",
        evidence_summary="every check failed after pr-31",
        confidence=0.9,
        justification="the previous version passed",
        target_deploy=TargetDeploy(pr_number=PR, commit_sha=sha, deployed_at=DEPLOYED_AT),
    )
    return PipelineResult(
        incident_id=request.incident_id,
        status="rollback_proposed",
        resolution_type="rollback",
        root_cause=spec.root_cause,
        confidence=0.9,
        rollback_spec=spec,
        reflexion_loops=0,
        tool_calls_used=2,
        tokens_used=10,
    )


def _escalated_result(request: PipelineInput) -> PipelineResult:
    return PipelineResult(
        incident_id=request.incident_id,
        status="escalated",
        resolution_type="escalated",
        root_cause="not sure",
        confidence=0.4,
        escalation=EscalationContext(
            hypothesis="not sure",
            evidence=["fetch_logs: checks fail"],
            confidence=0.4,
            missing=["x"],
        ),
        reflexion_loops=2,
        tool_calls_used=4,
        tokens_used=10,
    )


def _run(
    request: PipelineInput,
    result: PipelineResult,
    *,
    suspect: SuspectDeploy | None = None,
    raw_hypothesis: bool = False,
    raw_evidence: bool = False,
    timeline: tuple[TimelineEntry, ...] = (),
) -> TerminalRun:
    return TerminalRun(
        request=request,
        result=result,
        timeline=timeline,
        suspect=suspect,
        raw_hypothesis=raw_hypothesis,
        raw_evidence=raw_evidence,
    )


class FakeClient:
    """A recording stand-in for the LangFuse v3 client."""

    def __init__(self) -> None:
        self.datasets: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.create_dataset_error: Exception | None = None
        self.create_item_error: Exception | None = None

    def create_dataset(self, *, name: str, description: str | None = None) -> None:
        self.datasets.append({"name": name, "description": description})
        if self.create_dataset_error is not None:
            raise self.create_dataset_error

    def create_dataset_item(self, **kwargs: Any) -> None:
        if self.create_item_error is not None:
            raise self.create_item_error
        self.items.append(kwargs)


class RecordingWriter:
    def __init__(self, error: Exception | None = None) -> None:
        self.runs: list[TerminalRun] = []
        self.error = error

    async def write(self, run: TerminalRun) -> bool:
        if self.error is not None:
            raise self.error
        self.runs.append(run)
        return True


def _deps(
    writer: RecordingWriter | None = None,
    *,
    persist: bool = True,
    semantic: SemanticMemory | None = None,
) -> PipelineDeps:
    return PipelineDeps(
        settings=Settings(_env_file=None, sentinel_fake_llm=True),  # type: ignore[call-arg]
        semantic_memory=semantic or SemanticMemory(None),
        episodic_memory=EpisodicMemory(None, MagicMock()),
        persist=persist,
        dataset_writer=writer,
    )


@pytest.fixture(autouse=True)
def fresh_loggers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loggers bound after the current structlog config, so ``capture_logs`` sees them.

    A module-level logger caches the processors list in force at its first use; a lifespan or
    ``run_eval`` that ran earlier in the session reconfigures logging and leaves it stale.
    """
    monkeypatch.setattr(dataset_writer, "logger", get_logger("sentinel.eval.dataset_writer"))
    monkeypatch.setattr(orchestrator, "logger", get_logger("sentinel.agents.orchestrator"))


@pytest.fixture
def target_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(
        pool: asyncpg.Pool | None, service: str, target: TargetDeploy
    ) -> TargetDeployCheck:
        record = DeployRecord(
            pr_number=target.pr_number,
            commit_sha=SHA40,
            deployed_at=DEPLOYED_AT,
            deploy_status="failed",
            files_changed=[],
        )
        return TargetDeployCheck(deploy_id="deploy-1", record=record)

    monkeypatch.setattr(orchestrator, "check_rollback_target", check)


def _triage() -> FakeTurn:
    return FakeTurn(
        output=TriageResult(
            severity=Severity.P2,
            affected_service=SERVICE,
            is_duplicate=False,
            recommended_action="check the last deploy",
        )
    )


def _happy(script: FakeScript, request: PipelineInput) -> None:
    suspect = CorrelatedDeploy(
        pr_number=PR, commit_sha=SHA, deployed_at=DEPLOYED_AT, deploy_status="failed"
    )
    spec = {
        "incident_id": request.incident_id,
        "root_cause": "pr-31 changed GET / to return HTML",
        "evidence_summary": "every check failed after pr-31 deployed",
        "confidence": 0.9,
        "justification": "the previous version passed the same checks",
        "target_deploy": {
            "pr_number": PR,
            "commit_sha": SHA,
            "deployed_at": DEPLOYED_AT.isoformat(),
        },
    }
    script.on("triage", _triage())
    script.on(
        "analysis",
        FakeTurn(tool_calls=[FakeToolCall("fetch_logs", {"request": {"service": SERVICE}})]),
        FakeTurn(
            output=AnalysisResult(
                hypothesis="GET / returns HTML since pr-31",
                evidence=["fetch_logs: every check failed after pr-31"],
                suspect_deploy=suspect,
            )
        ),
    )
    script.on(
        REFLEXION_AGENT,
        reflexion_turn(
            0.85,
            deploy=SuspectDeploy(commit_sha=SHA, pr_number=PR),
            supporting_evidence=["checks fail since pr-31"],
        ),
    )
    script.on(
        "resolution",
        FakeTurn(tool_calls=[FakeToolCall("prepare_rollback_spec", {"request": spec})]),
        FakeTurn(output="Drafted the rollback of PR #31."),
    )
    script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))


# ── The item ──────────────────────────────────────────────────────────────────


def test_item_matches_the_runner_schema() -> None:
    request = _request()
    run = _run(request, _rollback_result(request))

    item = scenario_run_item(run)

    assert item is not None
    ScenarioRun.model_validate(item)
    assert set(item) == {"branch", "pr_number", "merge_sha", "incidents"}
    assert item["branch"] == "deployfail/03" and item["pr_number"] == PR
    assert item["merge_sha"] == SHA40
    (incident,) = item["incidents"]
    assert set(incident) == {
        "incident_id",
        "signal_type",
        "correlated_deploy",
        "pipeline_result",
        "trajectory",
    }
    assert incident["incident_id"] == request.incident_id
    assert incident["signal_type"] == "deploy_failure"
    assert incident["correlated_deploy"] == f"pr-{PR}-{SHA40}"
    assert incident["pipeline_result"]["status"] == "rollback_proposed"


async def test_no_branch_skips_and_logs() -> None:
    client = FakeClient()
    writer = LangfuseScenarioWriter(client=client)
    for request in (
        _request(branch=None),
        _request(branch="  "),
        _request(pr_details=False),
    ):
        run = _run(request, _rollback_result(request))
        assert scenario_run_item(run) is None
        with capture_logs() as logs:
            assert await writer.write(run) is False
        skipped = [e for e in logs if e["event"] == "scenario_run_item_skipped"]
        assert len(skipped) == 1
        assert skipped[0]["reason"] == "no pr_details.branch"
    assert client.items == [] and client.datasets == []


def test_invalid_merge_sha_is_none() -> None:
    request = _request(sha="xyz")
    item = scenario_run_item(_run(request, _rollback_result(request)))
    assert item is not None and item["merge_sha"] is None


def test_escalation_correlates_to_the_critique_deploy() -> None:
    request = _request("runtime_error")
    result = _escalated_result(request)
    assert correlated_deploy(
        _run(request, result, suspect=SuspectDeploy(commit_sha="a1b2c3d", pr_number=42))
    ) == ("pr-42-a1b2c3d")
    assert (
        correlated_deploy(
            _run(request, result, suspect=SuspectDeploy(commit_sha="a1b2c3d", pr_number=None))
        )
        is None
    )
    assert correlated_deploy(_run(request, result)) is None
    # A suspect that is not hex does not make a valid pr-<N>-<sha>.
    assert (
        correlated_deploy(
            _run(request, result, suspect=SuspectDeploy(commit_sha="not-a-sha", pr_number=42))
        )
        is None
    )


def test_the_alert_version_tag_is_never_the_correlation() -> None:
    request = _request("runtime_error")
    request.alert.tags.version = "pr-99-deadbee"
    item = scenario_run_item(_run(request, _escalated_result(request)))
    assert item is not None and item["incidents"][0]["correlated_deploy"] is None


def test_rollback_export_keeps_llm_text() -> None:
    request = _request()
    run = _run(request, _rollback_result(request))
    assert export_result(run) == run.result
    assert export_result(run).root_cause == "pr-31 broke GET /"


def test_export_strips_only_the_flagged_escalation_parts() -> None:
    request = _request("runtime_error")
    result = _escalated_result(request)
    both = export_result(_run(request, result, raw_hypothesis=True, raw_evidence=True))
    assert both.escalation is not None
    assert both.escalation.evidence == []
    assert both.escalation.hypothesis == WITHHELD_REPLY and both.root_cause == WITHHELD_REPLY
    assert both.escalation.missing == ["x"]
    evidence_only = export_result(_run(request, result, raw_evidence=True))
    assert evidence_only.escalation is not None
    assert evidence_only.escalation.evidence == [] and evidence_only.root_cause == "not sure"
    assert export_result(_run(request, result)) == result


def test_trajectory_is_the_status_only_timeline() -> None:
    request = _request()
    entries = (
        TimelineEntry(agent_name="analysis", action="fetch_logs", result_summary="ok"),
        TimelineEntry(agent_name="analysis", action="fetch_logs", result_summary="error: timeout"),
    )
    traj = trajectory(_run(request, _rollback_result(request), timeline=entries))
    assert traj["incident_id"] == request.incident_id
    assert traj["stm_context"]["status"] == "rollback_proposed"
    assert [e["result_summary"] for e in traj["stm_context"]["timeline"]] == [
        "ok",
        "error: timeout",
    ]
    assert set(traj) == {"incident_id", "stm_context"}


# ── The writer ────────────────────────────────────────────────────────────────


async def test_writer_creates_dataset_once_and_items_by_incident_id() -> None:
    client = FakeClient()
    writer = LangfuseScenarioWriter(client=client)
    requests = [_request(), _request()]
    for request in requests:
        with capture_logs() as logs:
            assert await writer.write(_run(request, _rollback_result(request))) is True
        assert any(e["event"] == "scenario_run_item_written" for e in logs)

    assert client.datasets == [
        {"name": DATASET_NAME, "description": dataset_writer.DATASET_DESCRIPTION}
    ]
    assert DATASET_NAME == "sentinel-scenario-runs"
    assert len(client.items) == 2
    for request, call in zip(requests, client.items, strict=True):
        assert call["dataset_name"] == DATASET_NAME
        assert call["id"] == request.incident_id
        assert call["source_trace_id"] == incident_trace_id(request.incident_id)
        assert call["metadata"] == {"correlation_id": request.correlation_id}
        ScenarioRun.model_validate(call["input"])


async def test_create_dataset_failure_still_writes_the_item() -> None:
    client = FakeClient()
    client.create_dataset_error = RuntimeError("already exists")
    request = _request()
    assert await LangfuseScenarioWriter(client=client).write(
        _run(request, _rollback_result(request))
    )
    assert len(client.items) == 1


async def test_client_failure_is_logged_never_raised() -> None:
    client = FakeClient()
    client.create_item_error = RuntimeError("down")
    request = _request()
    with capture_logs() as logs:
        written = await LangfuseScenarioWriter(client=client).write(
            _run(request, _rollback_result(request))
        )
    assert written is False
    (failed,) = [e for e in logs if e["event"] == "scenario_run_item_failed"]
    assert failed["error_type"] == "RuntimeError" and failed["error"] == "down"
    assert failed["incident_id"] == request.incident_id
    # The item itself is never logged.
    assert SHA40 not in json.dumps(logs, default=str)


def test_default_writer_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Settings(_env_file=None, sentinel_fake_llm=True)  # type: ignore[call-arg]
    live = Settings(_env_file=None, sentinel_fake_llm=False)  # type: ignore[call-arg]
    monkeypatch.setattr(dataset_writer, "langfuse_enabled", lambda: True)
    assert default_writer(fake) is None
    monkeypatch.setattr(dataset_writer, "langfuse_enabled", lambda: False)
    assert default_writer(live) is None
    monkeypatch.setattr(dataset_writer, "langfuse_enabled", lambda: True)
    writer = default_writer(live)
    assert isinstance(writer, LangfuseScenarioWriter)
    assert default_writer(live) is writer  # process-wide


# ── Through the pipeline ──────────────────────────────────────────────────────


async def test_pipeline_writes_one_item_at_the_terminal_state(target_ok: None) -> None:
    request = _request()
    writer = RecordingWriter()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _deps(writer))

    assert result.status == "rollback_proposed"
    (run,) = writer.runs
    assert run.result == result and run.request == request
    assert run.suspect == SuspectDeploy(commit_sha=SHA, pr_number=PR)
    assert run.raw_hypothesis is False and run.raw_evidence is False
    assert len(run.timeline) > 0


async def test_a_failing_writer_does_not_change_the_result(target_ok: None) -> None:
    request = _request()
    with fake_script() as script, capture_logs() as logs:
        _happy(script, request)
        result = await run_pipeline(request, _deps(RecordingWriter(error=RuntimeError("boom"))))
    assert result.status == "rollback_proposed"
    assert any(e["event"] == "scenario_run_item_failed" for e in logs)


async def test_persist_false_writes_nothing(target_ok: None) -> None:
    request = _request()
    writer = RecordingWriter()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _deps(writer, persist=False))
    assert result.status == "rollback_proposed"
    assert writer.runs == []


async def test_no_raw_tool_output_reaches_the_item() -> None:
    semantic = MagicMock(spec=SemanticMemory)
    semantic.query = AsyncMock(side_effect=KeyError(MARK))
    request = _request("deploy_failure")
    writer = RecordingWriter()
    with fake_script() as script:
        script.on(
            "triage",
            FakeTurn(
                tool_calls=[
                    FakeToolCall("get_service_metadata", {"request": {"service_name": MARK}})
                ]
            ),
            _triage(),
        )
        script.on("analysis", FakeTurn(output=f"unparsed {REPLY_MARK}"))
        script.on(REFLEXION_AGENT, reflexion_turn(0.3))
        result = await run_pipeline(request, _deps(writer, semantic=semantic))

    assert result.status == "escalated"
    assert result.escalation is not None
    # The raw clips really were produced...
    raw = json.dumps(result.model_dump(mode="json"))
    assert REPLY_MARK in result.root_cause
    assert MARK in raw
    # ...and the item carries neither of them.
    (run,) = writer.runs
    assert run.raw_hypothesis and run.raw_evidence
    item = scenario_run_item(run)
    assert item is not None
    text = json.dumps(item)
    assert MARK not in text and REPLY_MARK not in text
    exported = item["incidents"][0]["pipeline_result"]
    assert exported["escalation"]["evidence"] == []
    assert exported["root_cause"] == WITHHELD_REPLY
    assert exported["escalation"]["hypothesis"] == WITHHELD_REPLY


async def test_trajectory_is_status_only_after_a_scripted_call(target_ok: None) -> None:
    request = _request()
    writer = RecordingWriter()
    with fake_script() as script:
        _happy(script, request)
        await run_pipeline(request, _deps(writer))

    (run,) = writer.runs
    traj = trajectory(run)
    timeline = traj["stm_context"]["timeline"]
    assert any(e["action"] == "fetch_logs" for e in timeline)
    tool_entries = [e for e in timeline if e["action"] in {"fetch_logs", "prepare_rollback_spec"}]
    assert {e["action"] for e in tool_entries} == {"fetch_logs", "prepare_rollback_spec"}
    for entry in tool_entries:
        assert entry["result_summary"] == "ok" or entry["result_summary"].startswith("error")
    text = json.dumps(traj)
    assert "synthetics" not in text and '"logs"' not in text
