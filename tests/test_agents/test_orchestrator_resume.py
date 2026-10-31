"""Tests for the orchestrator's checkpoints and bounded resume (decision 2026-10-25 B).

Every run is on the fake LLM; the checkpoints go through the real ``CheckpointWriter`` into
a recording store (no database). The rollback-target cross-check is stubbed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from pydantic import JsonValue
from redis_stub import StubRedis, stub_handle

from sentinel.agents import orchestrator
from sentinel.agents.orchestrator import (
    PIPELINE_TIMEOUT,
    REFLEXION_MAX_LOOPS,
    PipelineDeps,
    ResumePoint,
    escalate_resumed,
    run_pipeline,
)
from sentinel.config import Settings
from sentinel.infra.status_events import STATUS_CHANNEL, StatusPublisher
from sentinel.memory.checkpoints import RESUME_FAILED, CheckpointWriter
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.alert import IncidentAlert
from sentinel.models.analysis import AnalysisResult
from sentinel.models.checkpoint import RunSnapshot
from sentinel.models.deploy import CorrelatedDeploy
from sentinel.models.incident import Severity, TriageResult
from sentinel.models.pipeline import PipelineInput
from sentinel.models.reflexion import FetchLogsCall, SuspectDeploy
from sentinel.models.tools import DeployRecord, LogFetchInput, TargetDeploy
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
PR = 42
DEPLOYED_AT = datetime.fromisoformat("2026-10-15T12:00:00+00:00")
DEPLOY = SuspectDeploy(commit_sha=SHA, pr_number=PR)

# ── Recording store + fixtures ────────────────────────────────────────────────


class _Store:
    """The CheckpointStore calls the writer makes, recorded in order."""

    def __init__(self) -> None:
        self.ops: list[tuple[Any, ...]] = []
        self.rows: dict[int, tuple[str, dict[str, JsonValue]]] = {}
        self.written = asyncio.Event()

    async def open_incident(self, request: PipelineInput, started_at: datetime) -> None:
        self.ops.append(("open", request.incident_id))

    async def write(
        self, incident_id: str, step: int, stage: str, state: Mapping[str, JsonValue]
    ) -> None:
        self.ops.append(("write", step, stage))
        self.rows[step] = (stage, dict(state))
        self.written.set()

    async def delete(self, incident_id: str) -> None:
        self.ops.append(("delete", incident_id))

    async def update_budget(self, incident_id: str, used: int, refused: int) -> None:
        """As CheckpointStore: raise the latest snapshot row's counts (never a marker's)."""
        self.ops.append(("budget", used, refused))
        steps = [s for s, (stage, _) in self.rows.items() if stage != "resumed"]
        if not steps:
            return
        stage, state = self.rows[max(steps)]
        state["tool_calls_used"] = max(int(str(state["tool_calls_used"])), used)
        state["tool_calls_refused"] = max(int(str(state["tool_calls_refused"])), refused)
        self.written.set()

    def latest(self) -> tuple[int, RunSnapshot]:
        """The highest-step snapshot row, as CheckpointStore.latest reads it."""
        step = max(s for s, (stage, _) in self.rows.items() if stage != "resumed")
        return step, self.snapshot(step)

    def stages(self) -> list[str]:
        return [op[2] for op in self.ops if op[0] == "write"]

    def snapshot(self, step: int) -> RunSnapshot:
        return RunSnapshot.model_validate(self.rows[step][1])

    def last(self, stage: str) -> tuple[int, RunSnapshot]:
        step = max(s for s, (st, _) in self.rows.items() if st == stage)
        return step, self.snapshot(step)


def _deps(store: _Store, **settings: Any) -> tuple[PipelineDeps, CheckpointWriter]:
    writer = CheckpointWriter(store)  # type: ignore[arg-type]
    episodic = MagicMock(spec=EpisodicMemory)
    episodic.store_incident = AsyncMock()
    deps = PipelineDeps(
        settings=Settings(_env_file=None, sentinel_fake_llm=True, **settings),  # type: ignore[call-arg]
        semantic_memory=SemanticMemory(None),
        episodic_memory=episodic,
        checkpoints=writer,
    )
    return deps, writer


def _request() -> PipelineInput:
    return PipelineInput(
        incident_id=str(uuid.uuid4()),
        alert=IncidentAlert(title="synthetics.http.failed", severity="critical", service=SERVICE),
        signal_type="runtime_error",
        correlation_id=str(uuid.uuid4()),
    )


def _triage() -> FakeTurn:
    return FakeTurn(
        output=TriageResult(
            severity=Severity.P2,
            affected_service=SERVICE,
            is_duplicate=False,
            recommended_action="check the last deploy",
        )
    )


def _analysis(*, delay: float = 0.0) -> list[FakeTurn]:
    suspect = CorrelatedDeploy(
        pr_number=PR, commit_sha=SHA, deployed_at=DEPLOYED_AT, deploy_status="succeeded"
    )
    return [
        FakeTurn(
            tool_calls=[FakeToolCall("fetch_logs", {"request": {"service": SERVICE}})],
            delay=delay,
        ),
        FakeTurn(
            output=AnalysisResult(
                hypothesis="GET / returns HTML since pr-42",
                evidence=["fetch_logs: every check failed after pr-42"],
                suspect_deploy=suspect,
            )
        ),
    ]


def _low() -> FakeTurn:
    return reflexion_turn(
        0.4,
        next_tool_call=FetchLogsCall(
            tool="fetch_logs", args=LogFetchInput(service=SERVICE, window_minutes=90)
        ),
        missing_or_contradictory=["no check read after the deploy"],
    )


def _high() -> FakeTurn:
    return reflexion_turn(0.85, deploy=DEPLOY, supporting_evidence=["checks fail since pr-42"])


def _rollback(request: PipelineInput) -> list[FakeTurn]:
    spec = {
        "incident_id": request.incident_id,
        "root_cause": "pr-42 changed GET / to return HTML",
        "evidence_summary": "every check failed after pr-42 deployed",
        "confidence": 0.9,
        "justification": "the previous version passed the same checks",
        "target_deploy": {
            "pr_number": PR,
            "commit_sha": SHA,
            "deployed_at": DEPLOYED_AT.isoformat(),
        },
    }
    return [
        FakeTurn(tool_calls=[FakeToolCall("prepare_rollback_spec", {"request": spec})]),
        FakeTurn(output="Drafted the rollback of PR #42."),
    ]


def _runs(script: FakeScript, agent: str) -> int:
    return sum(1 for name, _ in script.calls if name == agent)


@pytest.fixture(autouse=True)
def target_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(
        pool: asyncpg.Pool | None, service: str, target: TargetDeploy
    ) -> TargetDeployCheck:
        record = DeployRecord(
            pr_number=target.pr_number,
            commit_sha=target.commit_sha,
            deploy_status="succeeded",
            deployed_at=target.deployed_at,
            files_changed=["app/main.py"],
        )
        return TargetDeployCheck(deploy_id="deploy-1", record=record)

    monkeypatch.setattr(orchestrator, "check_rollback_target", check)


@pytest.fixture
def inputs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    real = orchestrator.call_with_fallback

    async def spy(agent: Any, input: Any, **kwargs: Any) -> Any:
        seen.append((agent.name, str(input)))
        return await real(agent, input, **kwargs)

    monkeypatch.setattr(orchestrator, "call_with_fallback", spy)
    return seen


# ── Checkpoints ───────────────────────────────────────────────────────────────


async def test_checkpoints_written_per_stage() -> None:
    store = _Store()
    deps, writer = _deps(store)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis(), *_analysis())
        script.on(REFLEXION_AGENT, _low(), _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, deps)
    await writer.flush(request.incident_id)

    assert result.status == "rollback_proposed"
    assert store.ops[0] == ("open", request.incident_id)
    assert store.stages() == [
        "accepted",
        "triage",
        "analysis",
        "reflexion",
        "analysis",
        "reflexion",
        "resolution",
    ]
    steps = [op[1] for op in store.ops if op[0] == "write"]
    assert steps == list(range(len(steps)))
    assert store.ops[-1] == ("delete", request.incident_id)  # discarded once stored
    # Each checkpoint is the full state after its stage.
    _, after_triage = store.last("triage")
    assert after_triage.triage is not None and after_triage.analysis is None
    assert [e.action for e in after_triage.timeline][-1] == "plan"
    assert store.snapshot(3).reflexion_loops == 1 and not store.snapshot(3).loop_done
    assert store.snapshot(5).loop_done
    _, resolution = store.last("resolution")
    assert resolution.draft is not None and resolution.attempt == 0


async def test_no_checkpoints_when_persist_false() -> None:
    store = _Store()
    deps, writer = _deps(store)
    deps = dataclasses.replace(deps, persist=False)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, deps)
    await writer.flush(request.incident_id)

    assert result.status == "rollback_proposed"
    assert store.ops == [] and len(writer) == 0


async def test_an_unstored_incident_keeps_its_checkpoints() -> None:
    store = _Store()
    deps, writer = _deps(store)
    deps.episodic_memory.store_incident.side_effect = RuntimeError("db down")  # type: ignore[attr-defined]
    request = _request()
    with fake_script():
        await run_pipeline(request, deps)
    await writer.flush(request.incident_id)
    assert not [op for op in store.ops if op[0] == "delete"]


# ── Resume ────────────────────────────────────────────────────────────────────


async def test_simulated_crash_resumes_once_and_caps_hold() -> None:
    store = _Store()
    deps, writer = _deps(store)
    request = _request()

    # Run 1: loop 1's critique sends analysis back; that second analysis hangs → the crash.
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis(), *_analysis(delay=60))
        script.on(REFLEXION_AGENT, _low())
        task = asyncio.create_task(run_pipeline(request, deps))
        while "reflexion" not in store.stages():
            store.written.clear()
            await asyncio.wait_for(store.written.wait(), 5)
        await asyncio.sleep(0.05)  # the hung analysis call is in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await writer.flush(request.incident_id)
    step, snap = store.last("reflexion")
    assert snap.reflexion_loops == 1 and not snap.loop_done
    assert snap.tool_calls_used == 1
    assert store.stages() == ["accepted", "triage", "analysis", "reflexion"]

    # Run 2: resumed after the restart (the marker would sit at step + 1).
    with fake_script() as script:
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, deps, resume=ResumePoint(snap, step + 2))
    await writer.flush(request.incident_id)

    assert result.status == "rollback_proposed"
    assert _runs(script, "triage") == 0  # triage is not run again
    assert result.reflexion_loops == 1 <= REFLEXION_MAX_LOOPS  # continued, not reset
    assert snap.tool_calls_used <= result.tool_calls_used == 3
    assert result.tool_calls_used <= deps.settings.sentinel_max_tool_calls
    (incident,), _ = deps.episodic_memory.store_incident.call_args  # type: ignore[attr-defined]
    actions = [e.action for e in incident.timeline]
    assert actions.count("resume") == 1 and actions.count("plan") == 1
    assert incident.created_at == snap.started_at  # MTTR from the original start
    # The resumed run's checkpoints continue after the marker's step.
    resumed_steps = [op[1] for op in store.ops if op[0] == "write"][4:]
    assert resumed_steps[0] == step + 2
    assert store.ops[-1] == ("delete", request.incident_id)


async def test_restored_loop_count_still_caps() -> None:
    store = _Store()
    deps, writer = _deps(store)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis(), *_analysis())
        script.on(REFLEXION_AGENT, _low(), _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        await run_pipeline(request, deps)
    await writer.flush(request.incident_id)
    loop_snap = store.snapshot(3).model_copy(update={"reflexion_loops": REFLEXION_MAX_LOOPS})

    with fake_script() as script:
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _low())  # wants more: but no loop is left
        result = await run_pipeline(request, deps, resume=ResumePoint(loop_snap, 10))

    assert result.status == "escalated"
    assert result.reflexion_loops == REFLEXION_MAX_LOOPS
    assert _runs(script, "analysis") == 2 and _runs(script, REFLEXION_AGENT) == 1


async def test_restored_tool_budget_still_caps() -> None:
    store = _Store()
    deps, writer = _deps(store, sentinel_max_tool_calls=3)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        await run_pipeline(request, deps)
    await writer.flush(request.incident_id)
    _, triage_snap = store.last("triage")
    spent = triage_snap.model_copy(update={"tool_calls_used": 3})

    with fake_script() as script:
        script.on("analysis", *_analysis())
        result = await run_pipeline(request, deps, resume=ResumePoint(spent, 10))

    assert result.status == "escalated"
    assert result.tool_calls_used == 3  # restored, and nothing more could run
    assert result.escalation is not None
    assert "tool-call budget (3 per incident) was exhausted" in result.escalation.missing[0]


def _fetches(n: int) -> list[FakeToolCall]:
    return [FakeToolCall("fetch_logs", {"request": {"service": SERVICE}}) for _ in range(n)]


async def test_budget_checkpointed_on_every_tool_call() -> None:
    store = _Store()
    deps, writer = _deps(store)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, deps)
    await writer.flush(request.incident_id)

    budget = [op for op in store.ops if op[0] == "budget"]
    assert [op[1] for op in budget] == list(range(1, result.tool_calls_used + 1))
    # Each call's update lands before the checkpoint of the stage it was made in.
    analysis_write = next(i for i, op in enumerate(store.ops) if op[:3] == ("write", 2, "analysis"))
    first_budget = next(i for i, op in enumerate(store.ops) if op[0] == "budget")
    assert first_budget < analysis_write


async def test_crash_mid_stage_then_resume_never_exceeds_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    granted: list[bool] = []
    real_take = orchestrator.ToolBudget.take

    def counting(self: orchestrator.ToolBudget) -> bool:
        allowed = real_take(self)
        granted.append(allowed)
        return allowed

    monkeypatch.setattr(orchestrator.ToolBudget, "take", counting)
    store = _Store()
    deps, writer = _deps(store)
    limit = deps.settings.sentinel_max_tool_calls
    assert limit == 20
    request = _request()

    # Run 1: analysis makes 6 calls, then its next model call hangs → the crash, mid-stage.
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", FakeTurn(tool_calls=_fetches(6)), FakeTurn(output="x", delay=60))
        task = asyncio.create_task(run_pipeline(request, deps))
        deadline = time.monotonic() + 10
        while not store.rows or store.latest()[1].tool_calls_used < 6:
            assert time.monotonic() < deadline, store.ops
            store.written.clear()
            await asyncio.wait_for(store.written.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await writer.flush(request.incident_id)
    assert store.stages() == ["accepted", "triage"]  # no analysis checkpoint: mid-stage
    step, snap = store.latest()
    assert snap.stage == "triage"
    assert snap.tool_calls_used == 6  # the interrupted stage's calls are counted

    # Run 2: the resume asks for far more than is left.
    with fake_script() as script:
        script.on("analysis", *(FakeTurn(tool_calls=_fetches(6)) for _ in range(5)))
        result = await run_pipeline(request, deps, resume=ResumePoint(snap, step + 2))

    assert result.status == "escalated"
    assert result.tool_calls_used == limit
    assert sum(granted) == limit  # across both runs: never more than 20 calls ran
    assert result.escalation is not None
    assert f"tool-call budget ({limit} per incident) was exhausted" in result.escalation.missing[0]


async def test_resumed_deadline_is_the_first_start_plus_the_bound() -> None:
    store = _Store()
    deps, writer = _deps(store)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        await run_pipeline(request, deps)
    await writer.flush(request.incident_id)
    bound = timedelta(seconds=deps.settings.sentinel_pipeline_timeout_s)
    for step in store.rows:
        first = store.snapshot(step)
        assert first.deadline_at == first.started_at + bound  # persisted in every checkpoint
    _, triage_snap = store.last("triage")
    original = triage_snap.started_at + bound
    store.rows.clear()

    # Resumed under a changed setting: the persisted deadline wins, unchanged.
    resumed_deps = dataclasses.replace(
        deps,
        settings=Settings(_env_file=None, sentinel_fake_llm=True, sentinel_pipeline_timeout_s=600),  # type: ignore[call-arg]
    )
    with fake_script() as script:
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, resumed_deps, resume=ResumePoint(triage_snap, 20))
    await writer.flush(request.incident_id)

    assert result.status == "rollback_proposed"
    assert store.rows  # the resumed run checkpointed
    for step in store.rows:
        assert store.snapshot(step).deadline_at == original


async def test_resume_gets_only_the_time_left() -> None:
    store = _Store()
    deps, _ = _deps(store)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        await run_pipeline(request, deps)
    _, snap = store.last("triage")
    bound = deps.settings.sentinel_pipeline_timeout_s
    started = datetime.now(UTC) - timedelta(seconds=bound - 0.3)  # 0.3 s of the bound left
    late = snap.model_copy(
        update={"started_at": started, "deadline_at": started + timedelta(seconds=bound)}
    )

    with fake_script() as script:
        script.on("analysis", FakeTurn(output="x", delay=60))  # would outlast a fresh bound
        began = time.monotonic()
        result = await run_pipeline(request, deps, resume=ResumePoint(late, 10))

    assert time.monotonic() - began < 5
    assert result.status == "escalated"
    assert result.escalation is not None
    assert result.escalation.missing[0].startswith(PIPELINE_TIMEOUT)


async def test_resume_after_the_deadline_escalates_at_once() -> None:
    store = _Store()
    deps, _ = _deps(store)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        await run_pipeline(request, deps)
    _, snap = store.last("triage")
    # A snapshot from before deadline_at was persisted: derived from started_at + the bound.
    expired = snap.model_copy(
        update={"started_at": datetime.now(UTC) - timedelta(hours=1), "deadline_at": None}
    )

    with fake_script() as script:
        script.on("analysis", *_analysis())
        result = await run_pipeline(request, deps, resume=ResumePoint(expired, 10))

    assert result.status == "escalated"
    assert result.escalation is not None
    assert result.escalation.missing[0].startswith(PIPELINE_TIMEOUT)
    assert _runs(script, "analysis") == 0


async def test_resume_from_resolution_skips_resolution_agent() -> None:
    store = _Store()
    deps, writer = _deps(store)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        await run_pipeline(request, deps)
    await writer.flush(request.incident_id)
    step, snap = store.last("resolution")

    with fake_script() as script:
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, deps, resume=ResumePoint(snap, step + 2))

    assert result.status == "rollback_proposed"
    assert _runs(script, "resolution") == 0  # the restored draft is used
    assert _runs(script, REVERIFICATION_AGENT) == 1  # and verified again
    assert _runs(script, "triage") == _runs(script, "analysis") == 0
    assert result.rollback_spec is not None and result.rollback_spec.target_deploy.commit_sha == SHA
    (incident,), _ = deps.episodic_memory.store_incident.call_args  # type: ignore[attr-defined]
    actions = [e.action for e in incident.timeline]
    assert actions.count("decision_gate") == 1  # not noted twice
    assert "target_check" in actions  # the deterministic checks re-run


async def test_resume_from_verify_uses_correction(inputs: list[tuple[str, str]]) -> None:
    store = _Store()
    deps, writer = _deps(store)
    request = _request()
    reason = "the evidence summary omits the failed assertion"
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request), *_rollback(request))
        script.on(
            REVERIFICATION_AGENT,
            reverification_turn("FAIL", reason),
            reverification_turn("FAIL", "still wrong"),
        )
        first = await run_pipeline(request, deps)
    await writer.flush(request.incident_id)
    assert first.status == "escalated"
    verify_steps = [s for s, (stage, _) in store.rows.items() if stage == "verify"]
    assert len(verify_steps) == 2
    snap = store.snapshot(verify_steps[0])
    assert (snap.attempt, snap.correction, snap.draft) == (1, reason, None)
    inputs.clear()

    with fake_script() as script:
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, deps, resume=ResumePoint(snap, 20))

    assert result.status == "rollback_proposed"
    resolutions = [text for name, text in inputs if name == "resolution"]
    assert len(resolutions) == 1
    assert "Correction from reverification (FAIL)" in resolutions[0]
    assert reason in resolutions[0]

    # Resumed after the last FAIL: no retry is left, the same escalation stands.
    last = store.snapshot(verify_steps[1])
    with fake_script() as script:
        again = await run_pipeline(request, deps, resume=ResumePoint(last, 30))
    assert _runs(script, "resolution") == 0
    assert again.escalation is not None
    assert again.escalation.missing[0] == "reverification failed after a retry: still wrong"


async def test_escalate_resumed_reason() -> None:
    store = _Store()
    deps, writer = _deps(store)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        await run_pipeline(request, deps)
    await writer.flush(request.incident_id)
    store.ops.clear()
    deps.episodic_memory.store_incident.reset_mock()  # type: ignore[attr-defined]
    _, snap = store.last("analysis")

    stub = StubRedis()
    publisher = StatusPublisher(stub_handle(stub))
    result = await escalate_resumed(ResumePoint(snap, 9), deps, publisher=publisher)
    await writer.flush(request.incident_id)
    await publisher.drain(2.0)

    assert result.status == "escalated"
    assert result.escalation is not None
    assert result.escalation.missing[0] == RESUME_FAILED == "resumed after restart failed"
    assert result.tool_calls_used == snap.tool_calls_used
    deps.episodic_memory.store_incident.assert_awaited_once()  # type: ignore[attr-defined]
    (incident,), kwargs = deps.episodic_memory.store_incident.call_args  # type: ignore[attr-defined]
    assert kwargs["outcome"].resolution == "escalated: resumed after restart failed"
    assert incident.created_at == snap.started_at
    assert store.ops == [("delete", request.incident_id)]
    # The terminal status goes out on sentinel:events, like every other terminal run.
    ((channel, message),) = stub.published
    assert channel == STATUS_CHANNEL == "sentinel:events"
    assert '"status": "escalated"' in message
    assert request.incident_id in message


async def test_cache_stats_annotated(monkeypatch: pytest.MonkeyPatch) -> None:
    annotate = MagicMock()
    log = MagicMock()
    monkeypatch.setattr(orchestrator, "annotate_trace", annotate)
    monkeypatch.setattr(orchestrator, "logger", log)
    store = _Store()
    deps, _ = _deps(store)
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        result = await run_pipeline(request, deps)

    expected = {
        "cache_llm_hit": 0,
        "cache_llm_miss": 0,
        "cache_tool_hit": 0,
        "cache_tool_miss": 0,
    }
    annotate.assert_called_once_with(expected)
    complete = [c for c in log.info.call_args_list if c.args == ("pipeline_complete",)]
    assert complete[0].kwargs["cache"] == expected
    assert complete[0].kwargs["resumed"] is False
    assert complete[0].kwargs["status"] == result.status
    assert _runs(script, "triage") == 1
