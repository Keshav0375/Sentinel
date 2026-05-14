"""Tests for GET /eval (HTML page) and GET /api/eval-report (JSON endpoint)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinel.api.eval_report import make_eval_report_router

# ── Helpers ───────────────────────────────────────────────────────────────────


def _sample_report() -> dict[str, Any]:
    """Minimal eval report matching the expected shape."""
    return {
        "generated_at": "2026-05-13T12:00:00Z",
        "results": [
            {
                "incident_id": "inc-001",
                "scenario_id": "bad_deploy_01",
                "dimension_scores": [
                    {"dimension": "triage_accuracy", "score": 4.5, "reasoning": "Correct"},
                    {"dimension": "root_cause_correctness", "score": 3.0, "reasoning": "Partial"},
                    {"dimension": "tool_efficiency", "score": 4.0, "reasoning": "Good"},
                    {"dimension": "mttr", "score": 3.5, "reasoning": "OK"},
                    {"dimension": "remediation_safety", "score": 5.0, "reasoning": "Perfect"},
                    {"dimension": "comms_quality", "score": 4.0, "reasoning": "Clear"},
                ],
                "total_score": 4.0,
                "judge_model": "llama-3.3-70b-versatile",
            },
            {
                "incident_id": "inc-002",
                "scenario_id": "db_pool_01",
                "dimension_scores": [
                    {"dimension": "triage_accuracy", "score": 2.0, "reasoning": "Wrong"},
                    {"dimension": "root_cause_correctness", "score": 1.5, "reasoning": "Missed"},
                    {"dimension": "tool_efficiency", "score": 3.0, "reasoning": "OK"},
                    {"dimension": "mttr", "score": 2.0, "reasoning": "Slow"},
                    {"dimension": "remediation_safety", "score": 4.0, "reasoning": "Safe"},
                    {"dimension": "comms_quality", "score": 2.5, "reasoning": "Vague"},
                ],
                "total_score": 2.5,
                "judge_model": "llama-3.3-70b-versatile",
            },
        ],
    }


def _report_app(report_path: Path) -> FastAPI:
    """Minimal FastAPI app with only the eval report router."""
    app = FastAPI()
    app.include_router(make_eval_report_router(report_path))
    return app


# ── GET /api/eval-report ─────────────────────────────────────────────────────


class TestEvalReportEndpoint:
    def test_returns_404_when_no_report(self, tmp_path: Path) -> None:
        app = _report_app(tmp_path / "missing.json")
        with TestClient(app) as client:
            resp = client.get("/api/eval-report")
        assert resp.status_code == 404
        assert "No eval report found" in resp.json()["detail"]

    def test_returns_200_with_report(self, tmp_path: Path) -> None:
        report_file = tmp_path / "eval_report.json"
        report_file.write_text(json.dumps(_sample_report()), encoding="utf-8")
        app = _report_app(report_file)
        with TestClient(app) as client:
            resp = client.get("/api/eval-report")
        assert resp.status_code == 200

    def test_returns_results_list(self, tmp_path: Path) -> None:
        report_file = tmp_path / "eval_report.json"
        report_file.write_text(json.dumps(_sample_report()), encoding="utf-8")
        app = _report_app(report_file)
        with TestClient(app) as client:
            data = client.get("/api/eval-report").json()
        assert "results" in data
        assert len(data["results"]) == 2

    def test_result_has_scenario_id(self, tmp_path: Path) -> None:
        report_file = tmp_path / "eval_report.json"
        report_file.write_text(json.dumps(_sample_report()), encoding="utf-8")
        app = _report_app(report_file)
        with TestClient(app) as client:
            data = client.get("/api/eval-report").json()
        for r in data["results"]:
            assert "scenario_id" in r

    def test_result_has_dimension_scores(self, tmp_path: Path) -> None:
        report_file = tmp_path / "eval_report.json"
        report_file.write_text(json.dumps(_sample_report()), encoding="utf-8")
        app = _report_app(report_file)
        with TestClient(app) as client:
            data = client.get("/api/eval-report").json()
        for r in data["results"]:
            assert "dimension_scores" in r
            assert len(r["dimension_scores"]) == 6

    def test_result_has_total_score(self, tmp_path: Path) -> None:
        report_file = tmp_path / "eval_report.json"
        report_file.write_text(json.dumps(_sample_report()), encoding="utf-8")
        app = _report_app(report_file)
        with TestClient(app) as client:
            data = client.get("/api/eval-report").json()
        for r in data["results"]:
            assert "total_score" in r
            assert 0.0 <= r["total_score"] <= 5.0

    def test_result_has_judge_model(self, tmp_path: Path) -> None:
        report_file = tmp_path / "eval_report.json"
        report_file.write_text(json.dumps(_sample_report()), encoding="utf-8")
        app = _report_app(report_file)
        with TestClient(app) as client:
            data = client.get("/api/eval-report").json()
        for r in data["results"]:
            assert "judge_model" in r

    def test_generated_at_present(self, tmp_path: Path) -> None:
        report_file = tmp_path / "eval_report.json"
        report_file.write_text(json.dumps(_sample_report()), encoding="utf-8")
        app = _report_app(report_file)
        with TestClient(app) as client:
            data = client.get("/api/eval-report").json()
        assert data["generated_at"] == "2026-05-13T12:00:00Z"

    def test_dimension_score_has_reasoning(self, tmp_path: Path) -> None:
        report_file = tmp_path / "eval_report.json"
        report_file.write_text(json.dumps(_sample_report()), encoding="utf-8")
        app = _report_app(report_file)
        with TestClient(app) as client:
            data = client.get("/api/eval-report").json()
        for ds in data["results"][0]["dimension_scores"]:
            assert "reasoning" in ds
            assert len(ds["reasoning"]) > 0

    def test_all_six_dimensions_present(self, tmp_path: Path) -> None:
        report_file = tmp_path / "eval_report.json"
        report_file.write_text(json.dumps(_sample_report()), encoding="utf-8")
        app = _report_app(report_file)
        with TestClient(app) as client:
            data = client.get("/api/eval-report").json()
        dims = {ds["dimension"] for ds in data["results"][0]["dimension_scores"]}
        expected = {
            "triage_accuracy",
            "root_cause_correctness",
            "tool_efficiency",
            "mttr",
            "remediation_safety",
            "comms_quality",
        }
        assert dims == expected


# ── GET /eval (HTML page) ────────────────────────────────────────────────────


class TestEvalPage:
    def test_get_eval_returns_html(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/eval")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_eval_page_contains_title(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/eval").text
        assert "Eval Results" in html

    def test_eval_page_contains_sentinel_brand(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/eval").text
        assert "Sentinel" in html

    def test_eval_page_contains_back_link(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/eval").text
        assert 'href="/"' in html

    def test_eval_page_references_api_endpoint(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/eval").text
        assert "/api/eval-report" in html

    def test_eval_page_contains_dimension_labels(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/eval").text
        assert "triage_accuracy" in html
        assert "root_cause_correctness" in html
        assert "tool_efficiency" in html
        assert "mttr" in html
        assert "remediation_safety" in html
        assert "comms_quality" in html

    def test_eval_page_contains_score_classes(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/eval").text
        assert "score-green" in html
        assert "score-amber" in html
        assert "score-red" in html

    def test_eval_page_contains_pass_fail(self) -> None:
        from sentinel.main import create_app

        app = create_app()
        with TestClient(app) as client:
            html = client.get("/eval").text
        assert "PASS" in html
        assert "FAIL" in html
