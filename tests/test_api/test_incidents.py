"""Tests for sentinel.api.incidents and sentinel.api.health endpoints."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinel.api.health import HealthResponse, make_health_router
from sentinel.api.incidents import (
    IncidentDetail,
    IncidentListItem,
    IncidentListResponse,
    make_incidents_router,
)
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.incident import IncidentStatus, TimelineEntry

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_alert(service: str = "api-gateway", alert_id: str = "alert-001") -> AlertPayload:
    return AlertPayload(
        alert_id=alert_id,
        source=AlertSource.DATADOG,
        service=service,
        metric="error_rate",
        threshold=0.05,
        current_value=0.34,
        severity=AlertSeverity.CRITICAL,
    )


def _make_test_app(stm: ShortTermMemory) -> FastAPI:
    app = FastAPI()
    app.include_router(make_incidents_router(stm))
    app.include_router(make_health_router(stm))
    return app


# ── GET /health ───────────────────────────────────────────────────────────────


def test_health_returns_200() -> None:
    stm = ShortTermMemory()
    client = TestClient(_make_test_app(stm))
    response = client.get("/health")
    assert response.status_code == 200


def test_health_response_shape() -> None:
    stm = ShortTermMemory()
    client = TestClient(_make_test_app(stm))
    data = client.get("/health").json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "active_incidents" in data


def test_health_active_incidents_zero_when_empty() -> None:
    stm = ShortTermMemory()
    client = TestClient(_make_test_app(stm))
    data = client.get("/health").json()
    assert data["active_incidents"] == 0


def test_health_active_incidents_reflects_stm() -> None:
    stm = ShortTermMemory()
    stm.create("inc-aaaaaaaa", _make_alert())
    stm.create("inc-bbbbbbbb", _make_alert(alert_id="alert-002"))
    client = TestClient(_make_test_app(stm))
    data = client.get("/health").json()
    assert data["active_incidents"] == 2


def test_health_no_stm_returns_zero_incidents() -> None:
    """Health router works without STM — active_incidents defaults to 0."""
    app = FastAPI()
    app.include_router(make_health_router(None))
    client = TestClient(app)
    data = client.get("/health").json()
    assert data["active_incidents"] == 0
    assert data["status"] == "ok"


def test_health_response_model_valid() -> None:
    resp = HealthResponse(status="ok", version="0.1.0", active_incidents=3)
    assert resp.status == "ok"
    assert resp.active_incidents == 3


# ── GET /incidents ────────────────────────────────────────────────────────────


def test_list_incidents_returns_200() -> None:
    stm = ShortTermMemory()
    client = TestClient(_make_test_app(stm))
    response = client.get("/incidents")
    assert response.status_code == 200


def test_list_incidents_empty_when_no_active() -> None:
    stm = ShortTermMemory()
    client = TestClient(_make_test_app(stm))
    data = client.get("/incidents").json()
    assert data["incidents"] == []
    assert data["total"] == 0


def test_list_incidents_returns_all_active() -> None:
    stm = ShortTermMemory()
    stm.create("inc-11111111", _make_alert(service="api-gateway", alert_id="a1"))
    stm.create("inc-22222222", _make_alert(service="payment-service", alert_id="a2"))
    stm.create("inc-33333333", _make_alert(service="auth-service", alert_id="a3"))

    client = TestClient(_make_test_app(stm))
    data = client.get("/incidents").json()

    assert data["total"] == 3
    assert len(data["incidents"]) == 3


def test_list_incidents_item_fields() -> None:
    stm = ShortTermMemory()
    stm.create("inc-44444444", _make_alert(service="order-service", alert_id="a4"))

    client = TestClient(_make_test_app(stm))
    incidents = client.get("/incidents").json()["incidents"]

    assert len(incidents) == 1
    item = incidents[0]
    assert item["incident_id"] == "inc-44444444"
    assert item["service"] == "order-service"
    assert item["status"] == "triage"
    assert item["alert_severity"] == "critical"


def test_list_incidents_status_reflects_stm_update() -> None:
    stm = ShortTermMemory()
    stm.create("inc-55555555", _make_alert())
    stm.update_context("inc-55555555", status=IncidentStatus.INVESTIGATING)

    client = TestClient(_make_test_app(stm))
    item = client.get("/incidents").json()["incidents"][0]
    assert item["status"] == "investigating"


def test_list_incidents_response_model_valid() -> None:
    item = IncidentListItem(
        incident_id="inc-test0001",
        service="api-gateway",
        status="triage",
        alert_severity="critical",
    )
    resp = IncidentListResponse(incidents=[item], total=1)
    assert resp.total == 1
    assert resp.incidents[0].service == "api-gateway"


# ── GET /incidents/{id} ───────────────────────────────────────────────────────


def test_get_incident_returns_200_for_known_id() -> None:
    stm = ShortTermMemory()
    stm.create("inc-66666666", _make_alert())
    client = TestClient(_make_test_app(stm))
    response = client.get("/incidents/inc-66666666")
    assert response.status_code == 200


def test_get_incident_returns_404_for_unknown_id() -> None:
    stm = ShortTermMemory()
    client = TestClient(_make_test_app(stm))
    response = client.get("/incidents/inc-does-not-exist")
    assert response.status_code == 404


def test_get_incident_detail_fields() -> None:
    stm = ShortTermMemory()
    alert = _make_alert(service="payment-service", alert_id="a-detail")
    stm.create("inc-77777777", alert)

    client = TestClient(_make_test_app(stm))
    data = client.get("/incidents/inc-77777777").json()

    assert data["incident_id"] == "inc-77777777"
    assert data["status"] == "triage"
    assert data["alert"]["service"] == "payment-service"
    assert data["timeline"] == []
    assert data["triage_result"] is None
    assert data["log_analysis"] is None
    assert data["deploy_correlation"] is None
    assert data["remediation_plan"] is None
    assert data["slack_summary"] is None


def test_get_incident_detail_with_timeline_entry() -> None:
    stm = ShortTermMemory()
    stm.create("inc-88888888", _make_alert())
    entry = TimelineEntry(
        agent_name="triage_agent",
        action="get_service_metadata",
        result_summary="api-gateway: critical tier, team-platform",
    )
    stm.add_timeline_entry("inc-88888888", entry)

    client = TestClient(_make_test_app(stm))
    data = client.get("/incidents/inc-88888888").json()

    assert len(data["timeline"]) == 1
    assert data["timeline"][0]["agent_name"] == "triage_agent"
    assert data["timeline"][0]["action"] == "get_service_metadata"


def test_get_incident_detail_with_triage_result() -> None:
    stm = ShortTermMemory()
    stm.create("inc-99999999", _make_alert())
    stm.update_context(
        "inc-99999999",
        triage_result={"severity": "P1", "affected_service": "api-gateway"},
        status=IncidentStatus.INVESTIGATING,
    )

    client = TestClient(_make_test_app(stm))
    data = client.get("/incidents/inc-99999999").json()

    assert data["triage_result"] == {"severity": "P1", "affected_service": "api-gateway"}
    assert data["status"] == "investigating"


def test_get_incident_detail_model_valid() -> None:
    alert = _make_alert()
    detail = IncidentDetail(
        incident_id="inc-test0001",
        alert=alert,
        status="triage",
        timeline=[],
        triage_result=None,
        log_analysis=None,
        deploy_correlation=None,
        remediation_plan=None,
        slack_summary=None,
    )
    assert detail.incident_id == "inc-test0001"
    assert detail.alert.service == "api-gateway"


def test_get_incident_404_detail_message() -> None:
    stm = ShortTermMemory()
    client = TestClient(_make_test_app(stm))
    data = client.get("/incidents/inc-missing").json()
    assert "not found" in data["detail"].lower()


def test_list_and_detail_consistent_ids() -> None:
    """IDs in list response must be fetchable via detail endpoint."""
    stm = ShortTermMemory()
    stm.create("inc-aaaabbbb", _make_alert(alert_id="al-1"))
    stm.create("inc-ccccdddd", _make_alert(service="auth-service", alert_id="al-2"))

    client = TestClient(_make_test_app(stm))
    listed_ids = [i["incident_id"] for i in client.get("/incidents").json()["incidents"]]

    for inc_id in listed_ids:
        response = client.get(f"/incidents/{inc_id}")
        assert response.status_code == 200
        assert response.json()["incident_id"] == inc_id
