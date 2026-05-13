"""Tests for sentinel.api.webhooks — AlertDeduplicator and POST /webhooks/alert."""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinel.api.webhooks import AlertDeduplicator, _new_incident_id, make_alert_router
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_payload(
    alert_id: str = "alert-001",
    service: str = "api-gateway",
    severity: AlertSeverity = AlertSeverity.CRITICAL,
) -> AlertPayload:
    return AlertPayload(
        alert_id=alert_id,
        source=AlertSource.DATADOG,
        service=service,
        metric="error_rate",
        threshold=0.05,
        current_value=0.34,
        severity=severity,
    )


def _make_test_app(
    deduplicator: AlertDeduplicator | None = None,
    stm: ShortTermMemory | None = None,
    pipeline_fn: object = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        make_alert_router(
            deduplicator=deduplicator or AlertDeduplicator(),
            short_term_memory=stm or ShortTermMemory(),
            pipeline_fn=pipeline_fn,  # type: ignore[arg-type]
        )
    )
    return app


def _post_alert(client: TestClient, payload: AlertPayload) -> object:
    """POST an AlertPayload to /webhooks/alert with correct Content-Type."""
    return client.post(
        "/webhooks/alert",
        json=payload.model_dump(mode="json"),
    )


# ── AlertDeduplicator ─────────────────────────────────────────────────────────


def test_dedup_check_unknown_alert() -> None:
    dedup = AlertDeduplicator()
    assert dedup.check("unknown-alert") is None


def test_dedup_record_and_check_within_ttl() -> None:
    dedup = AlertDeduplicator()
    dedup.record("alert-abc", "inc-12345678")
    result = dedup.check("alert-abc")
    assert result == "inc-12345678"


def test_dedup_check_different_alert_ids() -> None:
    dedup = AlertDeduplicator()
    dedup.record("alert-a", "inc-aaaaaaaa")
    dedup.record("alert-b", "inc-bbbbbbbb")
    assert dedup.check("alert-a") == "inc-aaaaaaaa"
    assert dedup.check("alert-b") == "inc-bbbbbbbb"


def test_dedup_check_expired_entry() -> None:
    dedup = AlertDeduplicator()
    dedup.TTL_SECONDS = 0.01  # type: ignore[assignment]
    dedup.record("alert-xyz", "inc-xxxxxxxx")
    time.sleep(0.05)  # wait past TTL
    assert dedup.check("alert-xyz") is None


def test_dedup_check_expired_evicts_entry() -> None:
    dedup = AlertDeduplicator()
    dedup.TTL_SECONDS = 0.01  # type: ignore[assignment]
    dedup.record("alert-xyz", "inc-xxxxxxxx")
    assert dedup.size == 1
    time.sleep(0.05)
    dedup.check("alert-xyz")  # triggers eviction
    assert dedup.size == 0


def test_dedup_size_reflects_cache_entries() -> None:
    dedup = AlertDeduplicator()
    assert dedup.size == 0
    dedup.record("alert-1", "inc-11111111")
    assert dedup.size == 1
    dedup.record("alert-2", "inc-22222222")
    assert dedup.size == 2


def test_dedup_evict_expired_removes_old_entries() -> None:
    dedup = AlertDeduplicator()
    dedup.TTL_SECONDS = 0.01  # type: ignore[assignment]
    dedup.record("alert-old", "inc-oooooooo")
    time.sleep(0.05)
    dedup.TTL_SECONDS = 60.0  # type: ignore[assignment]  # reset for fresh entry
    dedup.record("alert-new", "inc-nnnnnnnn")
    dedup.TTL_SECONDS = 0.01  # type: ignore[assignment]  # expire old
    time.sleep(0.05)
    evicted = dedup.evict_expired()
    assert evicted == 2  # both expired under 0.01s TTL
    assert dedup.size == 0


def test_dedup_evict_expired_keeps_fresh_entries() -> None:
    dedup = AlertDeduplicator()
    dedup.record("alert-fresh", "inc-ffffffff")
    evicted = dedup.evict_expired()
    assert evicted == 0
    assert dedup.size == 1


def test_dedup_record_overwrites_existing() -> None:
    """Recording the same alert_id twice replaces the entry."""
    dedup = AlertDeduplicator()
    dedup.record("alert-x", "inc-11111111")
    dedup.record("alert-x", "inc-22222222")
    assert dedup.check("alert-x") == "inc-22222222"
    assert dedup.size == 1


def test_dedup_ttl_default() -> None:
    assert AlertDeduplicator.TTL_SECONDS == 60.0


# ── _new_incident_id ──────────────────────────────────────────────────────────


def test_new_incident_id_format() -> None:
    inc_id = _new_incident_id()
    assert inc_id.startswith("inc-")
    assert len(inc_id) == len("inc-") + 8


def test_new_incident_id_is_unique() -> None:
    ids = {_new_incident_id() for _ in range(100)}
    assert len(ids) == 100


# ── POST /webhooks/alert — route handler ──────────────────────────────────────


def test_receive_alert_returns_202() -> None:
    client = TestClient(_make_test_app())
    response = _post_alert(client, _make_payload())
    assert response.status_code == 202


def test_receive_alert_returns_ack_with_ids() -> None:
    payload = _make_payload(alert_id="alert-unique-001")
    client = TestClient(_make_test_app())
    response = _post_alert(client, payload)
    data = response.json()
    assert data["alert_id"] == "alert-unique-001"
    assert data["incident_id"].startswith("inc-")
    assert "received_at" in data


def test_receive_alert_new_alert_creates_incident_in_stm() -> None:
    stm = ShortTermMemory()
    payload = _make_payload(alert_id="alert-stm-test")
    client = TestClient(_make_test_app(stm=stm))
    _post_alert(client, payload)
    assert stm.active_count == 1


def test_receive_alert_dedup_returns_same_incident_id() -> None:
    """Duplicate alert within TTL returns the same incident_id."""
    dedup = AlertDeduplicator()
    payload = _make_payload(alert_id="alert-dedup-test")
    client = TestClient(_make_test_app(deduplicator=dedup))

    resp1 = _post_alert(client, payload)
    resp2 = _post_alert(client, payload)
    assert resp1.status_code == 202
    assert resp2.status_code == 202
    assert resp1.json()["incident_id"] == resp2.json()["incident_id"]


def test_receive_alert_dedup_only_creates_one_incident() -> None:
    """Duplicate alert must not create a second incident in STM."""
    dedup = AlertDeduplicator()
    stm = ShortTermMemory()
    payload = _make_payload(alert_id="alert-one-incident")
    client = TestClient(_make_test_app(deduplicator=dedup, stm=stm))

    _post_alert(client, payload)
    _post_alert(client, payload)
    assert stm.active_count == 1


def test_receive_alert_distinct_alerts_create_separate_incidents() -> None:
    stm = ShortTermMemory()
    client = TestClient(_make_test_app(stm=stm))

    for i in range(3):
        _post_alert(client, _make_payload(alert_id=f"alert-distinct-{i}"))

    assert stm.active_count == 3


def test_receive_alert_triggers_pipeline_for_new_alert() -> None:
    """pipeline_fn must be queued as a background task for new alerts."""
    calls: list[tuple[str, str]] = []

    async def fake_pipeline(incident_id: str, alert: AlertPayload) -> None:
        calls.append((incident_id, alert.alert_id))

    payload = _make_payload(alert_id="alert-pipeline-new")
    client = TestClient(_make_test_app(pipeline_fn=fake_pipeline))
    _post_alert(client, payload)
    # TestClient runs background tasks synchronously before returning
    assert len(calls) == 1
    assert calls[0][1] == "alert-pipeline-new"


def test_receive_alert_does_not_trigger_pipeline_for_duplicate() -> None:
    """pipeline_fn must NOT be queued for duplicate alerts within TTL."""
    calls: list[str] = []

    async def fake_pipeline(incident_id: str, alert: AlertPayload) -> None:
        calls.append(incident_id)

    dedup = AlertDeduplicator()
    payload = _make_payload(alert_id="alert-pipeline-dedup")
    client = TestClient(_make_test_app(deduplicator=dedup, pipeline_fn=fake_pipeline))

    _post_alert(client, payload)
    _post_alert(client, payload)
    assert len(calls) == 1  # pipeline triggered once only


def test_receive_alert_no_pipeline_fn_still_returns_202() -> None:
    """When pipeline_fn is None, the route still returns 202."""
    payload = _make_payload(alert_id="alert-no-pipeline")
    client = TestClient(_make_test_app(pipeline_fn=None))
    response = _post_alert(client, payload)
    assert response.status_code == 202


def test_receive_alert_invalid_payload_returns_422() -> None:
    """Malformed JSON body must return 422 Unprocessable Entity."""
    client = TestClient(_make_test_app())
    response = client.post(
        "/webhooks/alert",
        json={"not": "a valid AlertPayload"},
    )
    assert response.status_code == 422


def test_receive_alert_stm_incident_id_matches_ack() -> None:
    """The incident_id in AlertAck must match what's stored in STM."""
    stm = ShortTermMemory()
    payload = _make_payload(alert_id="alert-id-match")
    client = TestClient(_make_test_app(stm=stm))

    response = _post_alert(client, payload)
    incident_id = response.json()["incident_id"]
    assert incident_id in stm.active_incident_ids
