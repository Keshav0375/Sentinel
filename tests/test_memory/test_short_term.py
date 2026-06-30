"""Tests for sentinel.memory.short_term.ShortTermMemory."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.incident import IncidentStatus, TimelineEntry

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_alert(service: str = "payment-service") -> AlertPayload:
    return AlertPayload(
        source=AlertSource.DATADOG,
        service=service,
        metric="error_rate",
        threshold=0.05,
        current_value=0.34,
        severity=AlertSeverity.CRITICAL,
    )


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


def test_create_initialises_result_fields_none(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    ctx = stm.get_context(INC)
    for field in (
        "triage_result",
        "log_analysis",
        "deploy_correlation",
        "remediation_plan",
        "slack_summary",
    ):
        assert ctx[field] is None, f"Expected {field} to be None"


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
        "triage_result",
        "log_analysis",
        "deploy_correlation",
        "remediation_plan",
        "slack_summary",
        "status",
    }
    assert expected.issubset(ctx.keys())


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
    stm.update_context(INC, triage_result={"severity": "P1"})
    ctx = stm.get_context(INC)
    assert ctx["alert"] is alert  # untouched
    assert ctx["log_analysis"] is None  # untouched


def test_update_context_sets_log_analysis(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    analysis = {"error_patterns": ["NullPointerException"], "hypothesis": "bad deploy"}
    stm.update_context(INC, log_analysis=analysis)
    assert stm.get_context(INC)["log_analysis"] == analysis


def test_update_context_sets_remediation_plan(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    plan = {"action_type": "rollback", "deploy_id": "deploy-abc123"}
    stm.update_context(INC, remediation_plan=plan)
    assert stm.get_context(INC)["remediation_plan"] == plan


def test_update_context_sets_slack_summary(stm: ShortTermMemory) -> None:
    stm.create(INC, _make_alert())
    stm.update_context(INC, slack_summary="Impact: payment service down")
    assert stm.get_context(INC)["slack_summary"] == "Impact: payment service down"


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

    # Triage step
    stm.add_timeline_entry(INC, _make_entry("triage", "classify_severity"))
    stm.update_context(INC, triage_result={"severity": "P1"}, status=IncidentStatus.INVESTIGATING)

    # Log analysis step
    stm.add_timeline_entry(INC, _make_entry("log_analyst", "fetch_logs"))
    stm.update_context(INC, log_analysis={"hypothesis": "null pointer"})

    # Deploy correlation step
    stm.add_timeline_entry(INC, _make_entry("deploy_correlator", "list_deploys"))
    stm.update_context(INC, deploy_correlation={"suspect_deploy": "deploy-abc"})

    # Remediation + HITL step
    stm.add_timeline_entry(INC, _make_entry("remediation", "draft_rollback"))
    stm.update_context(INC, status=IncidentStatus.PENDING_APPROVAL)

    # Resolution
    stm.update_context(INC, status=IncidentStatus.RESOLVED, slack_summary="Resolved.")
    stm.add_timeline_entry(INC, _make_entry("comms", "draft_slack_summary"))

    # Eval reads full context
    ctx = stm.get_context(INC)
    assert len(ctx["timeline"]) == 5
    assert ctx["status"] == IncidentStatus.RESOLVED
    assert ctx["triage_result"] == {"severity": "P1"}
    assert ctx["log_analysis"] == {"hypothesis": "null pointer"}
    assert ctx["slack_summary"] == "Resolved."

    # Cleanup
    stm.clear(INC)
    assert stm.active_count == 0
