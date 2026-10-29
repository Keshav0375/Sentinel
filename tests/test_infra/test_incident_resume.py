"""Tests for sentinel.infra.incident_resume — the bounded resume after a restart.

The unit tests drive :class:`IncidentResumer` over a recording store stub; the last test
runs the whole sweep against a throwaway database migrated to head (TEST_DATABASE_URL, see
tests/conftest.py; it skips without one, fails in CI) on the fake LLM.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import JsonValue
from redis_stub import StubRedis, stub_handle

from sentinel.agents import orchestrator
from sentinel.agents.orchestrator import PipelineDeps, ResumePoint, run_pipeline
from sentinel.api.webhooks import make_incident_router
from sentinel.config import Settings
from sentinel.infra.database import create_pool
from sentinel.infra.incident_resume import IncidentResumer
from sentinel.infra.incident_tasks import IncidentTasks
from sentinel.infra.status_events import StatusPublisher
from sentinel.memory.checkpoints import (
    RESUME_FAILED,
    CheckpointStore,
    CheckpointWriter,
)
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.memory.short_term import RedisStmMirror, ShortTermMemory
from sentinel.models.alert import IncidentAlert
from sentinel.models.analysis import AnalysisResult
from sentinel.models.checkpoint import Checkpoint, CheckpointStage, RunSnapshot
from sentinel.models.deploy import CorrelatedDeploy
from sentinel.models.incident import Severity, TimelineEntry, TriageResult
from sentinel.models.pipeline import PipelineInput
from sentinel.models.reflexion import SuspectDeploy
from sentinel.models.tools import DeployRecord, TargetDeploy
from sentinel.providers import (
    REFLEXION_AGENT,
    REVERIFICATION_AGENT,
    FakeToolCall,
    FakeTurn,
    fake_script,
    reflexion_turn,
    reverification_turn,
)
from sentinel.tools.deploy_checker import TargetDeployCheck

SERVICE = "sentinel-watchtower"
SHA = "a1b2c3d"
T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def _request() -> PipelineInput:
    return PipelineInput(
        incident_id=str(uuid.uuid4()),
        alert=IncidentAlert(title="synthetics.http.failed", severity="critical", service=SERVICE),
        signal_type="runtime_error",
        correlation_id=str(uuid.uuid4()),
    )


def _snapshot(request: PipelineInput, stage: CheckpointStage = "triage") -> RunSnapshot:
    return RunSnapshot(
        request=request,
        started_at=T0,
        stage=stage,
        timeline=[TimelineEntry(agent_name="orchestrator", action="plan", result_summary="p")],
        models_used=["anthropic/claude-haiku-4-5"],
        reflexion_loops=0,
        parse_failures=0,
        unparsed_critiques=0,
        tool_calls_used=0,
        tool_calls_refused=0,
        escalation_drafted=False,
        unparsed_hypothesis=False,
        fallback_evidence=False,
        triage=TriageResult(
            severity=Severity.P2,
            affected_service=SERVICE,
            is_duplicate=False,
            recommended_action="check the last deploy",
        ),
        analysis=None,
        critique=None,
        guidance=None,
        evidence=[],
        gathered=[],
    )


class _Store:
    """The CheckpointStore API over dicts; ``events`` records writes in order."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.in_progress: set[str] = set()
        self.rows: dict[str, dict[int, tuple[str, dict[str, JsonValue]]]] = {}
        self.abandoned: dict[str, str] = {}
        self.fail_write = False

    def put(self, incident_id: str, step: int, snap: RunSnapshot) -> None:
        self.in_progress.add(incident_id)
        self.rows.setdefault(incident_id, {})[step] = (snap.stage, snap.model_dump(mode="json"))

    async def stuck_incidents(self) -> list[str]:
        return sorted(self.in_progress)

    async def is_in_progress(self, incident_id: str) -> bool:
        return incident_id in self.in_progress

    async def latest(self, incident_id: str) -> Checkpoint | None:
        rows = {s: r for s, r in self.rows.get(incident_id, {}).items() if r[0] != "resumed"}
        if not rows:
            return None
        step = max(rows)
        stage, state = rows[step]
        return Checkpoint(
            incident_id=incident_id,
            step=step,
            stage=stage,  # type: ignore[arg-type]
            state=state,
            created_at=T0,
        )

    async def last_step(self, incident_id: str) -> int | None:
        rows = self.rows.get(incident_id, {})
        return max(rows) if rows else None

    async def was_resumed(self, incident_id: str) -> bool:
        return any(r[0] == "resumed" for r in self.rows.get(incident_id, {}).values())

    async def write(
        self, incident_id: str, step: int, stage: str, state: Mapping[str, JsonValue]
    ) -> None:
        if self.fail_write:
            raise ConnectionError("db down")
        self.events.append(f"write:{stage}:{step}")
        self.rows.setdefault(incident_id, {})[step] = (stage, dict(state))

    async def mark_abandoned(self, incident_id: str, reason: str) -> None:
        self.abandoned[incident_id] = reason
        self.in_progress.discard(incident_id)


def _deps() -> PipelineDeps:
    episodic = MagicMock(spec=EpisodicMemory)
    episodic.store_incident = AsyncMock()
    return PipelineDeps(
        settings=Settings(_env_file=None, sentinel_fake_llm=True),  # type: ignore[call-arg]
        semantic_memory=SemanticMemory(None),
        episodic_memory=episodic,
    )


class _RunFn:
    """Records each call (synchronously, as tasks.start sees it) and runs nothing long."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[PipelineInput, ResumePoint | None]] = []

    def __call__(self, request: PipelineInput, resume: ResumePoint | None = None) -> Any:
        self.events.append("run")
        self.calls.append((request, resume))

        async def run() -> None:
            await asyncio.sleep(0)

        return run()


def _resumer(
    store: _Store,
    tasks: IncidentTasks,
    run_fn: _RunFn,
    stm: ShortTermMemory | None = None,
    deps: PipelineDeps | None = None,
    **kwargs: Any,
) -> IncidentResumer:
    the_deps = deps or _deps()
    return IncidentResumer(
        store,  # type: ignore[arg-type]
        tasks,
        run_fn,
        lambda: the_deps,
        stm or ShortTermMemory(),
        **kwargs,
    )


# ── Unit ──────────────────────────────────────────────────────────────────────


async def test_sweep_resumes_once_writes_marker_first() -> None:
    events: list[str] = []
    store = _Store(events)
    request = _request()
    store.put(request.incident_id, 0, _snapshot(request, "accepted"))
    store.put(request.incident_id, 1, _snapshot(request, "triage"))
    tasks = IncidentTasks(2)
    run_fn = _RunFn(events)
    stm = ShortTermMemory()
    deps = _deps()
    resumer = _resumer(store, tasks, run_fn, stm, deps)

    assert await resumer.sweep() == [request.incident_id]

    assert events == ["write:resumed:2", "run"]  # the marker before the task
    ((resumed_request, point),) = run_fn.calls
    assert resumed_request == request
    assert point is not None and point.next_step == 3 and point.snapshot.stage == "triage"
    assert stm.get_context(request.incident_id)["alert"] == request.alert
    await asyncio.gather(*tasks)

    # A second restart: the marker is there, so the incident escalates instead.
    assert await resumer.sweep() == []
    assert len(run_fn.calls) == 1
    deps.episodic_memory.store_incident.assert_awaited_once()  # type: ignore[attr-defined]
    _, kwargs = deps.episodic_memory.store_incident.call_args  # type: ignore[attr-defined]
    assert kwargs["outcome"].resolution == f"escalated: {RESUME_FAILED}"


async def test_no_checkpoint_marks_abandoned() -> None:
    store = _Store()
    incident_id = str(uuid.uuid4())
    store.in_progress.add(incident_id)
    run_fn = _RunFn([])
    resumer = _resumer(store, IncidentTasks(), run_fn)

    assert await resumer.resume_one(incident_id) == "abandoned"

    assert store.abandoned == {
        incident_id: "escalated: resumed after restart failed (no checkpoint)"
    }
    assert run_fn.calls == []


async def test_not_in_progress_is_skipped() -> None:
    store = _Store()
    resumer = _resumer(store, IncidentTasks(), _RunFn([]))
    assert await resumer.resume_one(str(uuid.uuid4())) == "skipped"


async def test_skips_running_and_at_capacity() -> None:
    store = _Store()
    running, waiting = _request(), _request()
    store.put(running.incident_id, 0, _snapshot(running))
    store.put(waiting.incident_id, 0, _snapshot(waiting))
    tasks = IncidentTasks(1)
    gate = asyncio.Event()
    tasks.start(running.incident_id, gate.wait())  # type: ignore[arg-type]
    run_fn = _RunFn([])
    resumer = _resumer(store, tasks, run_fn)

    assert await resumer.resume_one(running.incident_id) == "skipped"  # running here
    assert await resumer.resume_one(waiting.incident_id) == "skipped"  # at capacity
    assert run_fn.calls == []
    assert not await store.was_resumed(waiting.incident_id)  # retried next time, unbounded
    gate.set()
    await asyncio.gather(*tasks)

    assert await resumer.resume_one(waiting.incident_id) == "resumed"
    await asyncio.gather(*tasks)
    await tasks.shutdown(0.1)
    assert await resumer.resume_one(running.incident_id) == "skipped"  # shutting down


async def test_marker_write_failure_skips_resume() -> None:
    store = _Store()
    request = _request()
    store.put(request.incident_id, 0, _snapshot(request))
    store.fail_write = True
    run_fn = _RunFn([])
    resumer = _resumer(store, IncidentTasks(), run_fn)

    assert await resumer.resume_one(request.incident_id) == "skipped"
    assert run_fn.calls == []


async def test_concurrent_resumes_of_one_incident_start_one_run() -> None:
    store = _Store()
    request = _request()
    store.put(request.incident_id, 0, _snapshot(request))
    tasks = IncidentTasks()
    run_fn = _RunFn([])
    resumer = _resumer(store, tasks, run_fn)

    outcomes = await asyncio.gather(
        resumer.resume_one(request.incident_id), resumer.resume_one(request.incident_id)
    )

    assert sorted(outcomes) == ["resumed", "skipped"]
    assert len(run_fn.calls) == 1
    await asyncio.gather(*tasks)


async def test_resume_refills_timeline_and_publishes() -> None:
    stub = StubRedis()
    handle = stub_handle(stub)
    mirror = RedisStmMirror(handle, 60)
    request = _request()
    # What the dead pod mirrored before it went down.
    before = ShortTermMemory(mirror=mirror)
    before.create(request.incident_id, request.alert)
    before.add_timeline_entry(
        request.incident_id,
        TimelineEntry(agent_name="triage", action="completed", result_summary="ok"),
    )
    await mirror.drain(2.0)

    store = _Store()
    store.put(request.incident_id, 0, _snapshot(request))
    stm = ShortTermMemory(mirror=mirror)
    publisher = StatusPublisher(handle)
    tasks = IncidentTasks()
    resumer = _resumer(store, tasks, _RunFn([]), stm, stm_mirror=mirror, publisher=publisher)

    assert await resumer.resume_one(request.incident_id) == "resumed"
    await publisher.drain(2.0)
    await asyncio.gather(*tasks)

    assert [e.action for e in stm.get_timeline(request.incident_id)] == ["completed"]
    assert [m for c, m in stub.published if c == "sentinel:events"]
    assert '"status": "resumed"' in stub.published[0][1]


async def test_sweep_never_raises() -> None:
    store = MagicMock()
    store.stuck_incidents = AsyncMock(side_effect=ConnectionError("db down"))
    resumer = _resumer(store, IncidentTasks(), _RunFn([]))
    assert await resumer.sweep() == []


async def test_webhook_retry_triggers_resume() -> None:
    stored = str(uuid.uuid4())
    correlation_id = str(uuid.uuid4())
    episodic = MagicMock()
    episodic.find_incident_by_correlation_id = AsyncMock(return_value=stored)
    resumer = MagicMock()
    resumer.resume_one = AsyncMock(return_value="resumed")
    pipeline_fn = AsyncMock()
    app = FastAPI()
    app.include_router(
        make_incident_router(ShortTermMemory(), episodic, pipeline_fn, resumer=resumer)
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/incident",
            json={"signal_type": "runtime_error", "correlation_id": correlation_id, "title": "t"},
        )
    for _ in range(3):
        await asyncio.sleep(0)

    assert response.status_code == 202
    assert response.json()["status"] == "duplicate"
    assert response.json()["incident_id"] == stored
    resumer.resume_one.assert_awaited_once_with(stored)
    pipeline_fn.assert_not_called()


# ── End to end (Postgres) ─────────────────────────────────────────────────────


def _with_database(dsn: str, name: str) -> str:
    return urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))


def _execute(dsn: str, sql: str) -> None:
    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_go())


@pytest.fixture(scope="module")
def _module_db() -> Iterator[dict[str, str]]:
    holder: dict[str, str] = {}
    yield holder
    if holder:
        _execute(holder["server"], f'DROP DATABASE IF EXISTS "{holder["name"]}" WITH (FORCE)')


@pytest.fixture
def resume_dsn(
    pg_dsn: str,
    _module_db: dict[str, str],
    alembic_config: Callable[..., Config],
    seed_embedding_client: EmbeddingClient,
) -> str:
    if not _module_db:
        name = f"sentinel_resume_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"], seed_embedding_client), "head")
    _execute(_module_db["dsn"], "DELETE FROM incidents")
    return _module_db["dsn"]


@pytest.fixture
async def pool(resume_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(resume_dsn, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


class _Embedder:
    async def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 383


async def test_sweep_end_to_end_pg(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(
        _pool: asyncpg.Pool | None, service: str, target: TargetDeploy
    ) -> TargetDeployCheck:
        record = DeployRecord(
            pr_number=target.pr_number,
            commit_sha=target.commit_sha,
            deploy_status="succeeded",
            deployed_at=target.deployed_at,
            files_changed=["app/main.py"],
        )
        return TargetDeployCheck(deploy_id=str(uuid.uuid4()), record=record)

    monkeypatch.setattr(orchestrator, "check_rollback_target", check)
    store = CheckpointStore(pool)
    writer = CheckpointWriter(store)
    deps = PipelineDeps(
        settings=Settings(_env_file=None, sentinel_fake_llm=True),  # type: ignore[call-arg]
        semantic_memory=SemanticMemory(pool),
        episodic_memory=EpisodicMemory(pool, _Embedder()),  # type: ignore[arg-type]
        checkpoints=writer,
    )
    request = _request()
    # What the dead pod left: the placeholder and its checkpoints up to triage.
    await store.open_incident(request, T0)
    await store.write(
        request.incident_id, 0, "accepted", _snapshot(request, "accepted").model_dump(mode="json")
    )
    await store.write(
        request.incident_id, 1, "triage", _snapshot(request, "triage").model_dump(mode="json")
    )
    abandoned = _request()
    await store.open_incident(abandoned, T0)  # a placeholder with no checkpoint at all

    tasks = IncidentTasks()

    def run_fn(req: PipelineInput, resume: ResumePoint | None = None) -> Any:
        return run_pipeline(req, deps, resume=resume)

    resumer = IncidentResumer(store, tasks, run_fn, lambda: deps, ShortTermMemory())
    suspect = CorrelatedDeploy(
        pr_number=42, commit_sha=SHA, deployed_at=T0, deploy_status="succeeded"
    )
    spec = {
        "incident_id": request.incident_id,
        "root_cause": "pr-42 changed GET / to return HTML",
        "evidence_summary": "every check failed after pr-42 deployed",
        "confidence": 0.9,
        "justification": "the previous version passed the same checks",
        "target_deploy": {"pr_number": 42, "commit_sha": SHA, "deployed_at": T0.isoformat()},
    }
    with fake_script() as script:
        script.on(
            "analysis",
            FakeTurn(tool_calls=[FakeToolCall("fetch_logs", {"request": {"service": SERVICE}})]),
            FakeTurn(
                output=AnalysisResult(
                    hypothesis="GET / returns HTML since pr-42",
                    evidence=["checks fail"],
                    suspect_deploy=suspect,
                )
            ),
        )
        script.on(
            REFLEXION_AGENT,
            reflexion_turn(
                0.85,
                deploy=SuspectDeploy(commit_sha=SHA, pr_number=42),
                supporting_evidence=["checks fail since pr-42"],
            ),
        )
        script.on(
            "resolution",
            FakeTurn(tool_calls=[FakeToolCall("prepare_rollback_spec", {"request": spec})]),
            FakeTurn(output="Drafted."),
        )
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))

        resumed = await resumer.sweep()
        await asyncio.gather(*tasks)
    await writer.flush(request.incident_id)

    assert resumed == [request.incident_id]
    assert sum(1 for name, _ in script.calls if name == "triage") == 0
    row = await pool.fetchrow(
        "SELECT status, created_at, embedding IS NOT NULL AS embedded FROM incidents WHERE id = $1",
        uuid.UUID(request.incident_id),
    )
    assert row is not None
    assert row["status"] == "rollback_proposed" and row["embedded"]
    assert row["created_at"] == T0
    assert await pool.fetchval("SELECT count(*) FROM incident_checkpoints") == 0
    gone = await pool.fetchrow(
        "SELECT status, resolution FROM incidents WHERE id = $1", uuid.UUID(abandoned.incident_id)
    )
    assert gone is not None and gone["status"] == "escalated"
    assert gone["resolution"] == "escalated: resumed after restart failed (no checkpoint)"
    assert await store.stuck_incidents() == []
