"""Tests for GET / (dashboard HTML) and GET /api/scenarios."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinel.api.scenarios import make_scenarios_router
from sentinel.catalog import load_catalog

# ── Helpers ───────────────────────────────────────────────────────────────────


def _scenarios_app() -> FastAPI:
    """Minimal FastAPI app with only the scenarios router."""
    app = FastAPI()
    app.include_router(make_scenarios_router())
    return app


# ── GET /api/scenarios ────────────────────────────────────────────────────────


class TestScenariosEndpoint:
    def test_returns_the_catalog(self) -> None:
        with TestClient(_scenarios_app()) as client:
            resp = client.get("/api/scenarios")
        assert resp.status_code == 200
        data = resp.json()
        assert data == [t.model_dump(mode="json") for t in load_catalog()]
        assert len(data) == 30

    def test_each_item_has_exactly_the_catalog_fields(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        for item in data:
            assert set(item) == {
                "branch",
                "case",
                "fault",
                "expected_signal_type",
                "expected_resolution",
                "expected_failed_stage",
                "also_expected",
                "expected_culprit",
            }

    def test_case_ii_item_shape(self) -> None:
        with TestClient(_scenarios_app()) as client:
            data = client.get("/api/scenarios").json()
        item = next(i for i in data if i["branch"] == "deployfail/03")
        assert item["expected_failed_stage"] == "deploy"
        assert item["also_expected"] == []
        assert item["expected_culprit"] == "self"

    def test_is_read_only(self) -> None:
        with TestClient(_scenarios_app()) as client:
            assert client.post("/api/scenarios").status_code == 405


# ── GET / (dashboard HTML) ────────────────────────────────────────────────────


class TestDashboardRoute:
    @pytest.fixture(autouse=True)
    def _open_dashboard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SENTINEL_DEV_OPEN_DASHBOARD", "1")

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

    def test_dashboard_has_no_fire_control(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "fire-btn" not in html
        assert "fireScenario" not in html
        assert "Fire Scenario" not in html

    def test_dashboard_has_no_live_view(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        for gone in ("EventSource", "/events/", "resolve-modal", "inc-bar", "pipeline-flow"):
            assert gone not in html
        assert "Live Incident" not in html
        assert "<title>Sentinel — Incident Response</title>" in html

    def test_dashboard_offers_no_approval_path(self) -> None:
        """The revert PR on GitHub is the only gate: no approve button, modal or call."""
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "/approve" not in html
        assert "hitl" not in html.lower()
        assert "Approve" not in html

    def test_dashboard_posts_no_incident(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "/webhooks/incident" not in html
        assert "/webhooks/alert" not in html

    def test_dashboard_renders_catalog_fields(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "s.branch" in html
        assert "s.fault" in html
        assert "expected_signal_type" in html
        assert "failure_class" not in html

    def test_dashboard_references_api_scenarios(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/").text
        assert "/api/scenarios" in html
