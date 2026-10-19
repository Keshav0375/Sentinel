"""Tests for sentinel.memory.short_term.ShortTermMemory."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import IncidentAlert
from sentinel.models.incident import IncidentStatus, TimelineEntry

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_alert(service: str = "payment-service") -> IncidentAlert:
    return IncidentAlert(title="error_rate", severity="critical", service=service)


def _make_entry(agent: str = "triage", action: str = "classify") -> TimelineEntry:
    return TimelineEntry(
        timestamp=datetime.now(UTC),
        agent_name=agent,
        action=action,
        result_summary="done",
    )


@pytest.fixture()
def stm() -> ShortTermMemory:
    return ShortTermMemory()


INC = "inc-aabbccdd"


# ── Construction ──────────────────────────────────────────────────────────────


def test_initial_state_empty(stm: ShortTermMemory) -> None:
    assert stm.active_count == 0
    assert stm.active_incident_ids == []


# ── create ────────────────────────────────────────────────────────────────────


def test_create_adds_incident(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    assert stm.active_count == 1


def test_create_sets_incident_id(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    ctx = stm.get_context(INC)
    assert ctx["incident_id"] == INC


def test_create_stores_alert(stm: ShortTermMemory) -> None:
    alert = _make_alert()
    stm.create(INC, alert)
    ctx = stm.get_context(INC)
    assert ctx["alert"] is alert


def test_create_initialises_timeline_empty(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    assert stm.get_timeline(INC) == []


def test_create_initialises_status_triage(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    ctx = stm.get_context(INC)
    assert ctx["status"] == IncidentStatus.TRIAGE


def test_create_initialises_the_result_none(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    assert stm.get_context(INC)["result"] is None


def test_create_multiple_incidents_tracked_independently(stm: ShortTermMemory) -> None:
    stm.create("inc-001", _make_alert("api-gateway"))
    stm.create("inc-002", _make_alert("payment-service"))
    assert stm.active_count == 2
    assert stm.get_context("inc-001")["alert"].service == "api-gateway"
    assert stm.get_context("inc-002")["alert"].service == "payment-service"


def test_create_overwrites_existing_record(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    stm.add_timeline_entry(INC, _make_entry())
    # Re-create same ID — should reset to fresh state
    stm.create(INC, _make_alert())
    assert stm.get_timeline(INC) == []


# ── add_timeline_entry / get_timeline ─────────────────────────────────────────


def test_add_timeline_entry_appends(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    entry = _make_entry()
    stm.add_timeline_entry(INC, entry)
    assert len(stm.get_timeline(INC)) == 1


def test_add_timeline_entry_preserves_order(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    e1 = _make_entry("triage", "classify")
    e2 = _make_entry("log_analyst", "fetch_logs")
    e3 = _make_entry("deploy_correlator", "list_deploys")
    stm.add_timeline_entry(INC, e1)
    stm.add_timeline_entry(INC, e2)
    stm.add_timeline_entry(INC, e3)
    timeline = stm.get_timeline(INC)
    assert timeline[0].agent_name == "triage"
    assert timeline[1].agent_name == "log_analyst"
    assert timeline[2].agent_name == "deploy_correlator"


def test_add_timeline_entry_raises_for_unknown_incident(stm: ShortTermMemory) -> None:
    with pytest.raises(KeyError, match="inc-unknown"):
        stm.add_timeline_entry("inc-unknown", _make_entry())


def test_get_timeline_returns_defensive_copy(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    stm.add_timeline_entry(INC, _make_entry())
    copy1 = stm.get_timeline(INC)
    copy1.clear()  # mutate the returned list
    assert len(stm.get_timeline(INC)) == 1  # original unaffected


def test_get_timeline_raises_for_unknown_incident(stm: ShortTermMemory) -> None:
    with pytest.raises(KeyError, match="inc-unknown"):
        stm.get_timeline("inc-unknown")


def test_timeline_entries_are_timeline_entry_instances(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    stm.add_timeline_entry(INC, _make_entry())
    timeline = stm.get_timeline(INC)
    assert isinstance(timeline[0], TimelineEntry)


def test_multiple_entries_accumulated(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    for i in range(5):
        stm.add_timeline_entry(INC, _make_entry(action=f"step_{i}"))
    assert len(stm.get_timeline(INC)) == 5


# ── get_context ───────────────────────────────────────────────────────────────


def test_get_context_returns_dict(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    ctx = stm.get_context(INC)
    assert isinstance(ctx, dict)


def test_get_context_contains_expected_keys(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    ctx = stm.get_context(INC)
    expected = {
        "incident_id",
        "alert",
        "timeline",
        "result",
        "status",
    }
    assert set(ctx.keys()) == expected


def test_get_context_raises_for_unknown_incident(stm: ShortTermMemory) -> None:
    with pytest.raises(KeyError, match="inc-unknown"):
        stm.get_context("inc-unknown")


def test_get_context_reflects_timeline_updates(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    stm.add_timeline_entry(INC, _make_entry())
    ctx = stm.get_context(INC)
    assert len(ctx["timeline"]) == 1


# ── update_context ────────────────────────────────────────────────────────────


def test_update_context_sets_triage_result(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    result = {"severity": "P1", "service": "payment-service"}
    stm.update_context(INC, triage_result=result)
    assert stm.get_context(INC)["triage_result"] == result


def test_update_context_advances_status(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    stm.update_context(INC, status=IncidentStatus.INVESTIGATING)
    assert stm.get_context(INC)["status"] == IncidentStatus.INVESTIGATING


def test_update_context_multiple_fields_at_once(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    stm.update_context(
        INC,
        triage_result={"severity": "P2"},
        log_analysis={"hypothesis": "null pointer in auth"},
        status=IncidentStatus.INVESTIGATING,
    )
    ctx = stm.get_context(INC)
    assert ctx["triage_result"] == {"severity": "P2"}
    assert ctx["log_analysis"] == {"hypothesis": "null pointer in auth"}
    assert ctx["status"] == IncidentStatus.INVESTIGATING


def test_update_context_raises_for_unknown_incident(stm: ShortTermMemory) -> None:
    with pytest.raises(KeyError, match="inc-unknown"):
        stm.update_context("inc-unknown", status=IncidentStatus.RESOLVED)


def test_update_context_preserves_unmodified_fields(stm: ShortTermMemory) -> None:
    alert = _make_alert()
    stm.create(INC, alert)
    stm.update_context(INC, status=IncidentStatus.INVESTIGATING)
    ctx = stm.get_context(INC)
    assert ctx["alert"] is alert  # untouched
    assert ctx["result"] is None  # untouched


def test_update_context_sets_the_result(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    result = {"status": "rollback_proposed", "resolution_type": "rollback"}
    stm.update_context(INC, result=result, status=IncidentStatus.ROLLBACK_PROPOSED)
    assert stm.get_context(INC)["result"] == result


# ── clear ─────────────────────────────────────────────────────────────────────


def test_clear_removes_incident(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    stm.clear(INC)
    assert stm.active_count == 0


def test_clear_nonexistent_is_noop(stm: ShortTermMemory) -> None:
    stm.clear("inc-nonexistent")  # must not raise


def test_clear_makes_get_context_raise(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    stm.clear(INC)
    with pytest.raises(KeyError):
        stm.get_context(INC)


def test_clear_only_affects_target_incident(stm: ShortTermMemory) -> None:
    stm.create("inc-001", _make_alert())
    stm.create("inc-002", _make_alert())
    stm.clear("inc-001")
    assert stm.active_count == 1
    ctx = stm.get_context("inc-002")
    assert ctx["incident_id"] == "inc-002"


# ── active_incident_ids / active_count ───────────────────────────────────────


def test_active_incident_ids_sorted(stm: ShortTermMemory) -> None:
    stm.create("inc-ccc", _make_alert())
    stm.create("inc-aaa", _make_alert())
    stm.create("inc-bbb", _make_alert())
    assert stm.active_incident_ids == ["inc-aaa", "inc-bbb", "inc-ccc"]


def test_active_count_tracks_creates_and_clears(stm: ShortTermMemory) -> None:
    assert stm.active_count == 0
    stm.create("inc-001", _make_alert())
    assert stm.active_count == 1
    stm.create("inc-002", _make_alert())
    assert stm.active_count == 2
    stm.clear("inc-001")
    assert stm.active_count == 1


# ── Full lifecycle ────────────────────────────────────────────────────────────


def test_full_incident_lifecycle(stm: ShortTermMemory) -> None:
    """Simulate an end-to-end incident arc through short-term memory."""
    alert = _make_alert()
    stm.create(INC, alert)

    # Pipeline steps (the tracer appends one entry per tool call)
    stm.add_timeline_entry(INC, _make_entry("triage", "get_service_metadata"))
    stm.update_context(INC, status=IncidentStatus.INVESTIGATING)
    stm.add_timeline_entry(INC, _make_entry("analysis", "fetch_logs"))
    stm.add_timeline_entry(INC, _make_entry("analysis", "get_deploy_details"))
    stm.add_timeline_entry(INC, _make_entry("resolution", "prepare_rollback_spec"))

    # Terminal: a rollback proposed (a human merges the revert PR on GitHub)
    result = {"status": "rollback_proposed", "reflexion_loops": 1}
    stm.update_context(INC, status=IncidentStatus.ROLLBACK_PROPOSED, result=result)

    # Eval reads full context
    ctx = stm.get_context(INC)
    assert len(ctx["timeline"]) == 4
    assert ctx["status"] == IncidentStatus.ROLLBACK_PROPOSED
    assert ctx["result"] == result

    # Cleanup
    stm.clear(INC)
    assert stm.active_count == 0
