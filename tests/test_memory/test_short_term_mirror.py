"""Tests for the Redis write-through of short-term memory (memory.short_term.RedisStmMirror).

Redis is the local stub (tests/redis_stub.py): no network.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from redis_stub import StubRedis, stub_handle

from sentinel.config import STM_TTL_MARGIN_S, Settings
from sentinel.memory import short_term as stm_module
from sentinel.memory.short_term import RedisStmMirror, ShortTermMemory
from sentinel.models.alert import IncidentAlert
from sentinel.models.incident import IncidentStatus, TimelineEntry

ALERT = IncidentAlert(title="synthetics.http.failed", severity="critical", service="svc")


def _entry(action: str = "fetch_logs") -> TimelineEntry:
    return TimelineEntry(agent_name="analysis", action=action, result_summary="ok")


def _ttl() -> int:
    return int(Settings(_env_file=None).sentinel_pipeline_timeout_s) + STM_TTL_MARGIN_S  # type: ignore[call-arg]


def test_no_mirror_behaviour_unchanged() -> None:
    stm = ShortTermMemory()
    incident_id = str(uuid.uuid4())
    stm.create(incident_id, ALERT)
    stm.add_timeline_entry(incident_id, _entry())
    stm.update_context(incident_id, status=IncidentStatus.ESCALATED, result={"x": 1})

    context = stm.get_context(incident_id)
    assert context["alert"] is ALERT
    assert context["status"] is IncidentStatus.ESCALATED and context["result"] == {"x": 1}
    assert [e.action for e in stm.get_timeline(incident_id)] == ["fetch_logs"]
    assert stm.active_incident_ids == [incident_id] and stm.active_count == 1
    stm.clear(incident_id)
    assert stm.active_count == 0
    with pytest.raises(KeyError):
        stm.get_context(incident_id)


async def test_mirror_write_through_and_ttl() -> None:
    stub = StubRedis()
    mirror = RedisStmMirror(stub_handle(stub), _ttl())
    stm = ShortTermMemory(mirror=mirror)
    incident_id = str(uuid.uuid4())

    stm.create(incident_id, ALERT)
    stm.add_timeline_entry(incident_id, _entry())
    stm.update_context(incident_id, status=IncidentStatus.ROLLBACK_PROPOSED)
    # Reads stay local and synchronous: nothing awaited yet.
    assert stm.get_context(incident_id)["status"] is IncidentStatus.ROLLBACK_PROPOSED
    await mirror.drain(2.0)

    key = f"sentinel:stm:{incident_id}"
    state = json.loads(stub.data[key][0])
    assert state["incident_id"] == incident_id
    assert state["alert"]["service"] == "svc"
    assert state["status"] == "rollback_proposed"
    assert [e["action"] for e in state["timeline"]] == ["fetch_logs"]
    assert stub.ttls[key] == 900 + 600 == _ttl()
    # Coalesced: three mutations, at most two writes (the first, then the latest).
    assert 1 <= stub.calls.count("set") <= 2


async def test_mirror_clear_deletes_key() -> None:
    stub = StubRedis()
    mirror = RedisStmMirror(stub_handle(stub), 60)
    stm = ShortTermMemory(mirror=mirror)
    incident_id = str(uuid.uuid4())
    stm.create(incident_id, ALERT)
    await mirror.drain(2.0)
    assert f"sentinel:stm:{incident_id}" in stub.data

    stm.clear(incident_id)
    await mirror.drain(2.0)

    assert f"sentinel:stm:{incident_id}" not in stub.data
    assert stub.calls[-1] == "delete"


async def test_mirror_failure_never_breaks_stm(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(stm_module, "logger", log)
    stub = StubRedis()
    stub.fail = True
    mirror = RedisStmMirror(stub_handle(stub), 60)
    stm = ShortTermMemory(mirror=mirror)
    incident_id = str(uuid.uuid4())

    stm.create(incident_id, ALERT)
    stm.add_timeline_entry(incident_id, _entry())
    await mirror.drain(2.0)

    assert [e.action for e in stm.get_timeline(incident_id)] == ["fetch_logs"]
    events = [c.args[0] for c in log.warning.call_args_list]
    assert events and set(events) == {"stm_mirror_failed"}
    assert log.warning.call_args.kwargs == {
        "incident_id": incident_id,
        "error_type": "ConnectionError",
    }
    assert await mirror.load_timeline(incident_id) == []


def test_mirror_save_outside_a_loop_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(stm_module, "logger", log)
    stm = ShortTermMemory(mirror=RedisStmMirror(stub_handle(StubRedis()), 60))
    incident_id = str(uuid.uuid4())
    stm.create(incident_id, ALERT)  # no running loop: the mirror cannot schedule
    assert stm.active_count == 1
    assert log.warning.call_args.args == ("stm_mirror_failed",)


async def test_load_timeline_roundtrip_and_missing() -> None:
    stub = StubRedis()
    mirror = RedisStmMirror(stub_handle(stub), 60)
    stm = ShortTermMemory(mirror=mirror)
    incident_id = str(uuid.uuid4())
    stm.create(incident_id, ALERT)
    stm.add_timeline_entry(incident_id, _entry("fetch_logs"))
    stm.add_timeline_entry(incident_id, _entry("get_deploy_details"))
    await mirror.drain(2.0)

    timeline = await mirror.load_timeline(incident_id)
    assert timeline == stm.get_timeline(incident_id)

    assert await mirror.load_timeline(str(uuid.uuid4())) == []
    stub.data["sentinel:stm:bad"] = ("not json", None)
    assert await mirror.load_timeline("bad") == []
    stub.data["sentinel:stm:bad"] = (json.dumps({"timeline": [{"nope": 1}]}), None)
    assert await mirror.load_timeline("bad") == []


def test_mirror_rejects_a_non_positive_ttl() -> None:
    with pytest.raises(ValueError, match="ttl_s"):
        RedisStmMirror(stub_handle(StubRedis()), 0)
