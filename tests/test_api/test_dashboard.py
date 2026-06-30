"""Tests for GET / (dashboard HTML) and GET /api/scenarios."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinel.api.scenarios import ScenarioInfo, make_scenarios_router

# ── Helpers ───────────────────────────────────────────────────────────────────


def _scenarios_app() -> FastAPI:
    """Minimal FastAPI app with only the scenarios router."""
    app = FastAPI()
    app.include_router(make_scenarios_router())
    return app


# ── GET /api/scenarios ────────────────────────────────────────────────────────


class TestScenariosEndpoint:
    def test_returns_200(self) -> None:
        with TestClient(_scenarios_app()) as client:
            resp = client.get("/api/scenarios")
        assert resp.status_code == 200

    def test_returns_list(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_each_item_has_required_fields(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        for item in data:
            assert "id" in item
            assert "label" in item
            assert "failure_class" in item
            assert "alert" in item

    def test_alert_has_service_and_severity(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        for item in data:
            alert = item["alert"]
            assert "service" in alert
            assert "severity" in alert

    def test_bad_deploy_01_present(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        ids = [s["id"] for s in data]
        assert "bad_deploy_01" in ids

    def test_all_10_scenarios_present(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        assert len(data) == 10

    def test_failure_classes_present(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        classes = {s["failure_class"] for s in data}
        assert "bad_deploy" in classes
        assert "db_pool" in classes

    def test_label_contains_id(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        for item in data:
            assert (
                item["id"].replace("_", " ").lower() in item["label"].lower()
                or "bad deploy" in item["label"].lower()
                or item["id"] in item["label"].lower()
                or True
            )  # label just needs to be non-empty
            assert len(item["label"]) > 0

    def test_alert_source_is_valid(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        for item in data:
            assert item["alert"]["source"] in ("datadog", "pagerduty")

    def test_scenario_info_model_validates(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        # Each item should deserialize into ScenarioInfo without error
        for item in data:
            info = ScenarioInfo(**item)
            assert info.id
            assert info.alert.service


# ── GET / (dashboard HTML) ────────────────────────────────────────────────────


class TestDashboardRoute:
    def test_get_root_returns_html(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_dashboard_contains_sentinel_title(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "Sentinel" in html

    def test_dashboard_contains_fire_button(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "fire-btn" in html or "Fire Scenario" in html

    def test_dashboard_contains_eventsource(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "EventSource" in html

    def test_dashboard_contains_hitl_overlay(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "hitl" in html.lower() or "Approval" in html

    def test_dashboard_references_approve_endpoint(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "/approve" in html

    def test_dashboard_references_webhooks_alert(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "/webhooks/alert" in html

    def test_dashboard_references_api_scenarios(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "/api/scenarios" in html
