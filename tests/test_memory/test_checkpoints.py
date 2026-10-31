"""Tests for sentinel.memory.checkpoints — the placeholder row, checkpoints, the writer.

Tests taking ``pool`` run against a throwaway database migrated to head on the pgvector
container named by TEST_DATABASE_URL (see tests/conftest.py), emptied before each test and
dropped afterwards; they skip without it (fail in CI). The writer tests use a recording
store and need no database.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from pydantic import JsonValue

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.database import create_pool
from sentinel.memory import checkpoints as checkpoints_module
from sentinel.memory.checkpoints import (
    CHECKPOINT_WRITE_TIMEOUT_S,
    INCIDENT_IN_PROGRESS,
    RESUME_FAILED,
    CheckpointStore,
    CheckpointWriter,
)
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.alert import IncidentAlert
from sentinel.models.checkpoint import Checkpoint, CheckpointStage, RunSnapshot
from sentinel.models.incident import Incident, IncidentStatus, TimelineEntry
from sentinel.models.memory import IncidentOutcome
from sentinel.models.pipeline import PipelineInput
from sentinel.models.tools import EscalationContext, RollbackSpec, TargetDeploy

SERVICE = "sentinel-watchtower"
T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def _request() -> PipelineInput:
    return PipelineInput(
        incident_id=str(uuid.uuid4()),
        alert=IncidentAlert(title="synthetics.http.failed", severity="critical", service=SERVICE),
        signal_type="runtime_error",
        correlation_id=str(uuid.uuid4()),
    )


def _snapshot(
    request: PipelineInput, stage: CheckpointStage = "triage", **fields: Any
) -> RunSnapshot:
    values: dict[str, Any] = {
        "request": request,
        "started_at": T0,
        "stage": stage,
        "timeline": [TimelineEntry(agent_name="orchestrator", action="plan", result_summary="x")],
        "models_used": ["anthropic/claude-haiku-4-5"],
        "reflexion_loops": 1,
        "parse_failures": 0,
        "unparsed_critiques": 0,
        "tool_calls_used": 3,
        "tool_calls_refused": 0,
        "escalation_drafted": False,
        "unparsed_hypothesis": False,
        "fallback_evidence": False,
        "triage": None,
        "analysis": None,
        "critique": None,
        "guidance": None,
        "evidence": [("fetch_logs", '{"ok": true}')],
        "gathered": [("fetch_logs", '{"ok": true}')],
    }
    values.update(fields)
    return RunSnapshot(**values)


# ── Models ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "draft",
    [
        None,
        RollbackSpec(
            incident_id=str(uuid.uuid4()),
            root_cause="pr-42",
            evidence_summary="checks fail",
            confidence=0.9,
            justification="the previous version passed",
            target_deploy=TargetDeploy(pr_number=42, commit_sha="a1b2c3d", deployed_at=T0),
        ),
        EscalationContext(hypothesis="h", evidence=["e"], confidence=0.3, missing=["m"]),
    ],
)
def test_snapshot_round_trips_through_json(draft: RollbackSpec | EscalationContext | None) -> None:
    request = _request()
    snap = _snapshot(request, "resolution", draft=draft, attempt=1, correction="fix it")
    state: dict[str, JsonValue] = json.loads(json.dumps(snap.model_dump(mode="json")))
    row = Checkpoint(
        incident_id=request.incident_id, step=4, stage="resolution", state=state, created_at=T0
    )

    back = row.snapshot()
    assert back == snap
    assert type(back.draft) is type(draft)


# ── Database fixtures ─────────────────────────────────────────────────────────


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
def checkpoint_dsn(
    pg_dsn: str,
    _module_db: dict[str, str],
    alembic_config: Callable[..., Config],
    seed_embedding_client: EmbeddingClient,
) -> str:
    """A database at head, with no incidents (and so no checkpoints)."""
    if not _module_db:
        name = f"sentinel_ckpt_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"], seed_embedding_client), "head")
    _execute(_module_db["dsn"], "DELETE FROM incidents")
    return _module_db["dsn"]


@pytest.fixture
async def pool(checkpoint_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(checkpoint_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


class _Embedder:
    async def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 383


async def _row(pool: asyncpg.Pool, incident_id: str) -> asyncpg.Record:
    row = await pool.fetchrow("SELECT * FROM incidents WHERE id = $1", uuid.UUID(incident_id))
    assert row is not None
    return row


# ── CheckpointStore (Postgres) ────────────────────────────────────────────────


async def test_open_incident_inserts_in_progress_placeholder(pool: asyncpg.Pool) -> None:
    store = CheckpointStore(pool)
    request = _request()

    await store.open_incident(request, T0)

    row = await _row(pool, request.incident_id)
    assert row["status"] == INCIDENT_IN_PROGRESS == "in_progress"
    assert row["embedding"] is None and row["trajectory"] is None
    assert row["service"] == SERVICE and row["severity"] == "critical"
    assert row["correlation_id"] == uuid.UUID(request.correlation_id)
    assert row["created_at"] == T0
    assert await store.is_in_progress(request.incident_id)

    # A second open is a no-op: the row (and its created_at) is kept.
    await store.open_incident(request, T0 + timedelta(minutes=5))
    assert await pool.fetchval("SELECT count(*) FROM incidents") == 1
    assert (await _row(pool, request.incident_id))["created_at"] == T0


async def test_store_incident_overwrites_placeholder_keeps_created_at(pool: asyncpg.Pool) -> None:
    store = CheckpointStore(pool)
    request = _request()
    await store.open_incident(request, T0)
    episodic = EpisodicMemory(pool, _Embedder())  # type: ignore[arg-type]
    resolved = T0 + timedelta(minutes=3)
    incident = Incident(
        id=request.incident_id,
        alert=request.alert,
        status=IncidentStatus.ESCALATED,
        created_at=T0 + timedelta(minutes=1),  # a later start: the placeholder's is kept
        resolved_at=resolved,
    )
    outcome = IncidentOutcome(
        correlation_id=request.correlation_id,
        root_cause="unknown",
        root_cause_by="orchestrator",
        root_cause_confidence=0.0,
        resolution="escalated: x",
        resolution_by="orchestrator",
        resolution_type="escalated",
        reflexion_loops=0,
        langfuse_trace_id=uuid.UUID(request.incident_id).hex,
        pipeline_result={},
    )

    await episodic.store_incident(incident, outcome=outcome)

    row = await _row(pool, request.incident_id)
    assert row["status"] == "escalated"
    assert row["created_at"] == T0
    assert row["mttr_seconds"] == 180  # from the placeholder's start
    assert row["embedding"] is not None
    assert not await store.is_in_progress(request.incident_id)


async def test_write_latest_skips_resumed_marker(pool: asyncpg.Pool) -> None:
    store = CheckpointStore(pool)
    request = _request()
    await store.open_incident(request, T0)
    accepted = _snapshot(request, "accepted").model_dump(mode="json")
    triage_snapshot = _snapshot(request, "triage")
    triage = triage_snapshot.model_dump(mode="json")

    assert await store.latest(request.incident_id) is None
    assert await store.last_step(request.incident_id) is None
    assert not await store.was_resumed(request.incident_id)

    await store.write(request.incident_id, 0, "accepted", accepted)
    await store.write(request.incident_id, 1, "triage", triage)
    await store.write(request.incident_id, 2, "resumed", {"from_step": 1})
    # A step already written is kept as it is.
    await store.write(request.incident_id, 1, "analysis", accepted)

    latest = await store.latest(request.incident_id)
    assert latest is not None
    assert (latest.step, latest.stage) == (1, "triage")
    assert latest.snapshot() == triage_snapshot
    assert await store.was_resumed(request.incident_id)
    assert await store.last_step(request.incident_id) == 2


async def test_update_budget_raises_latest_snapshot_only(pool: asyncpg.Pool) -> None:
    store = CheckpointStore(pool)
    request = _request()
    await store.open_incident(request, T0)
    await store.update_budget(request.incident_id, 5, 0)  # no checkpoint yet: a no-op
    await store.write(
        request.incident_id, 0, "accepted", _snapshot(request, "accepted").model_dump(mode="json")
    )
    await store.write(
        request.incident_id, 1, "triage", _snapshot(request, "triage").model_dump(mode="json")
    )
    await store.write(request.incident_id, 2, "resumed", {"from_step": 1})

    await store.update_budget(request.incident_id, 7, 1)
    await store.update_budget(request.incident_id, 6, 0)  # out of order: never lowers

    latest = await store.latest(request.incident_id)
    assert latest is not None and latest.step == 1
    snap = latest.snapshot()
    assert (snap.tool_calls_used, snap.tool_calls_refused) == (7, 1)
    assert snap.reflexion_loops == 1  # every other field is the stage's own
    rows = await pool.fetch(
        "SELECT step, state FROM incident_checkpoints WHERE incident_id = $1 ORDER BY step",
        uuid.UUID(request.incident_id),
    )
    states = {row["step"]: json.loads(row["state"]) for row in rows}
    assert states[0]["tool_calls_used"] == 3  # an older checkpoint is untouched
    assert states[2] == {"from_step": 1}  # and so is the marker


async def test_stuck_incidents_lists_only_in_progress(pool: asyncpg.Pool) -> None:
    store = CheckpointStore(pool)
    first, second, done = _request(), _request(), _request()
    await store.open_incident(second, T0 + timedelta(minutes=1))
    await store.open_incident(first, T0)
    await store.open_incident(done, T0)
    await pool.execute(
        "UPDATE incidents SET status = 'rollback_proposed' WHERE id = $1",
        uuid.UUID(done.incident_id),
    )

    assert await store.stuck_incidents() == [first.incident_id, second.incident_id]


async def test_mark_abandoned_sets_escalated(pool: asyncpg.Pool) -> None:
    store = CheckpointStore(pool)
    request = _request()
    await store.open_incident(request, T0)
    reason = f"escalated: {RESUME_FAILED} (no checkpoint)"

    await store.mark_abandoned(request.incident_id, reason)

    row = await _row(pool, request.incident_id)
    assert row["status"] == "escalated" and row["resolution_type"] == "escalated"
    assert row["resolution"] == "escalated: resumed after restart failed (no checkpoint)"
    assert row["resolved_at"] is not None
    # Only a placeholder is closed: a stored row is left as it is.
    await pool.execute(
        "UPDATE incidents SET status = 'rollback_proposed', resolution = 'kept' WHERE id = $1",
        uuid.UUID(request.incident_id),
    )
    await store.mark_abandoned(request.incident_id, reason)
    assert (await _row(pool, request.incident_id))["resolution"] == "kept"


async def test_delete_removes_every_checkpoint(pool: asyncpg.Pool) -> None:
    store = CheckpointStore(pool)
    request = _request()
    await store.open_incident(request, T0)
    for step in range(3):
        await store.write(request.incident_id, step, "analysis", {"n": step})

    await store.delete(request.incident_id)

    assert await pool.fetchval("SELECT count(*) FROM incident_checkpoints") == 0
    assert await store.is_in_progress(request.incident_id)  # the placeholder stays


async def test_stored_incident_ids_and_list_exclude_in_progress(pool: asyncpg.Pool) -> None:
    store = CheckpointStore(pool)
    episodic = EpisodicMemory(pool, _Embedder())  # type: ignore[arg-type]
    placeholder, stored = _request(), _request()
    await store.open_incident(placeholder, T0)
    await store.open_incident(stored, T0)
    await pool.execute(
        "UPDATE incidents SET status = 'escalated' WHERE id = $1", uuid.UUID(stored.incident_id)
    )
    ids = [placeholder.incident_id, stored.incident_id]

    assert await episodic.stored_incident_ids(ids) == {stored.incident_id}
    assert [r.id for r in await episodic.list_incident_rows()] == [stored.incident_id]
    assert [r.id for r in await episodic.list_incidents()] == [stored.incident_id]
    in_progress = await episodic.list_incident_rows(status="in_progress")
    assert [r.id for r in in_progress] == [placeholder.incident_id]
    # The webhook's dedupe still finds the in-flight placeholder.
    found = await episodic.find_incident_by_correlation_id(placeholder.correlation_id)
    assert found == placeholder.incident_id


async def test_store_without_pool_raises_memory_error() -> None:
    store = CheckpointStore(None)
    request = _request()
    with pytest.raises(SentinelMemoryError, match="memory unavailable"):
        await store.open_incident(request, T0)
    with pytest.raises(SentinelMemoryError):
        await store.write(request.incident_id, 0, "accepted", {})
    with pytest.raises(SentinelMemoryError):
        await store.latest(request.incident_id)
    with pytest.raises(SentinelMemoryError):
        await store.stuck_incidents()


async def test_a_non_uuid_id_is_a_memory_error() -> None:
    with pytest.raises(SentinelMemoryError, match="not a UUID"):
        await CheckpointStore(MagicMock()).latest("inc-1")


# ── CheckpointWriter (recording store, no database) ───────────────────────────


class _RecordingStore:
    """Records the writer's store calls in order; can fail or hang selected operations."""

    def __init__(self) -> None:
        self.ops: list[tuple[str, Any]] = []
        self.fail: set[str] = set()
        self.hang: set[str] = set()

    async def _op(self, name: str, detail: Any) -> None:
        await asyncio.sleep(0)  # yield: a write is never synchronous with the call
        if name in self.hang:
            await asyncio.Event().wait()
        if name in self.fail:
            raise SentinelMemoryError(f"{name} failed")
        self.ops.append((name, detail))

    async def open_incident(self, request: PipelineInput, started_at: datetime) -> None:
        await self._op("open", request.incident_id)

    async def write(
        self, incident_id: str, step: int, stage: str, state: Mapping[str, JsonValue]
    ) -> None:
        await self._op("write", (step, stage, state["stage"]))

    async def delete(self, incident_id: str) -> None:
        await self._op("delete", incident_id)

    async def update_budget(self, incident_id: str, used: int, refused: int) -> None:
        await self._op("budget", (used, refused))


def _writer(store: _RecordingStore, **kwargs: Any) -> CheckpointWriter:
    return CheckpointWriter(store, **kwargs)  # type: ignore[arg-type]


async def test_writer_orders_and_discards() -> None:
    store = _RecordingStore()
    writer = _writer(store)
    request = _request()
    incident_id = request.incident_id

    assert writer.open(request, T0) is None  # type: ignore[func-returns-value]
    for step, stage in enumerate(("accepted", "triage", "analysis")):
        assert writer.save(incident_id, step, _snapshot(request, stage)) is None  # type: ignore[func-returns-value]
    writer.discard(incident_id)
    assert store.ops == []  # nothing ran yet: every call returned at once
    assert len(writer) == 1

    await writer.flush(incident_id)

    assert store.ops == [
        ("open", incident_id),
        ("write", (0, "accepted", "accepted")),
        ("write", (1, "triage", "triage")),
        ("write", (2, "analysis", "analysis")),
        ("delete", incident_id),
    ]
    assert len(writer) == 0


async def test_writer_budget_updates_run_in_order() -> None:
    store = _RecordingStore()
    writer = _writer(store)
    request = _request()
    incident_id = request.incident_id
    writer.open(request, T0)
    writer.save(incident_id, 0, _snapshot(request, "accepted"))
    writer.save_budget(incident_id, 1, 0)
    writer.save_budget(incident_id, 2, 0)
    writer.save(incident_id, 1, _snapshot(request, "triage"))
    writer.save_budget(incident_id, 3, 1)

    await writer.flush(incident_id)

    assert store.ops == [
        ("open", incident_id),
        ("write", (0, "accepted", "accepted")),
        ("budget", (1, 0)),
        ("budget", (2, 0)),
        ("write", (1, "triage", "triage")),
        ("budget", (3, 1)),
    ]


async def test_writer_budget_skipped_when_disabled() -> None:
    store = _RecordingStore()
    store.fail.add("open")
    writer = _writer(store)
    request = _request()
    writer.open(request, T0)
    await writer.flush(request.incident_id)
    writer.save_budget(request.incident_id, 1, 0)
    await writer.flush(request.incident_id)
    assert store.ops == []


async def test_writer_snapshot_is_taken_at_save_time() -> None:
    store = _RecordingStore()
    writer = _writer(store)
    request = _request()
    snap = _snapshot(request, "triage")
    writer.save(request.incident_id, 0, snap)
    snap.reflexion_loops = 9  # mutated after the call: the queued state is unaffected
    seen: list[Any] = []
    store.write = lambda incident_id, step, stage, state: _record(seen, state)  # type: ignore[method-assign]

    await writer.flush(request.incident_id)

    assert seen[0]["reflexion_loops"] == 1


async def _record(seen: list[Any], state: Any) -> None:
    seen.append(state)


async def test_writer_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(checkpoints_module, "logger", log)
    store = _RecordingStore()
    writer = _writer(store)
    request, other = _request(), _request()

    store.fail = {"write"}
    writer.open(other, T0)
    writer.save(other.incident_id, 0, _snapshot(other, "accepted"))
    await writer.flush(other.incident_id)
    failed = [c for c in log.warning.call_args_list if c.args == ("checkpoint_write_failed",)]
    assert failed[-1].kwargs == {
        "incident_id": other.incident_id,
        "op": "write",
        "error_type": "SentinelMemoryError",
    }

    # A failed placeholder insert disables the incident's later writes.
    store.fail = {"open"}
    writer.open(request, T0)
    writer.save(request.incident_id, 0, _snapshot(request, "accepted"))
    await writer.flush(request.incident_id)
    writer.save(request.incident_id, 1, _snapshot(request, "triage"))
    await writer.flush(request.incident_id)

    assert not [op for op in store.ops if op[0] == "write"]
    disabled = [
        c for c in log.warning.call_args_list if c.args == ("checkpoints_disabled_for_incident",)
    ]
    assert disabled[0].kwargs == {
        "incident_id": request.incident_id,
        "error_type": "SentinelMemoryError",
    }
    # Its discard still runs and clears the disabled mark.
    store.fail = set()
    writer.discard(request.incident_id)
    await writer.flush(request.incident_id)
    assert store.ops[-1] == ("delete", request.incident_id)


async def test_writer_bounds_each_operation() -> None:
    store = _RecordingStore()
    store.hang = {"write"}
    writer = _writer(store, timeout_s=0.05)
    request = _request()
    writer.save(request.incident_id, 0, _snapshot(request, "accepted"))
    writer.discard(request.incident_id)

    await asyncio.wait_for(writer.flush(request.incident_id), 2.0)

    assert store.ops == [("delete", request.incident_id)]  # the hung write timed out


async def test_writer_drain_cancels_slow_ops() -> None:
    store = _RecordingStore()
    store.hang = {"write"}
    writer = _writer(store)  # the default 10 s bound: only the drain ends it
    request = _request()
    writer.save(request.incident_id, 0, _snapshot(request, "accepted"))
    writer.save(request.incident_id, 1, _snapshot(request, "triage"))

    loop = asyncio.get_running_loop()
    started = loop.time()
    cancelled = await writer.drain(0.05)

    assert loop.time() - started < 2.0
    assert cancelled == [request.incident_id]
    assert len(writer) == 0
    assert await writer.drain(0.05) == []


def test_writer_rejects_a_non_positive_timeout() -> None:
    assert CHECKPOINT_WRITE_TIMEOUT_S == 10.0
    with pytest.raises(ValueError, match="positive"):
        _writer(_RecordingStore(), timeout_s=0)
