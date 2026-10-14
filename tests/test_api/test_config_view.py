"""Tests for GET /api/config — the read-only §4.7 model view."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinel.api.config_view import make_config_router
from sentinel.config import Settings

_DASHBOARD = Path(__file__).resolve().parents[2] / "src" / "sentinel" / "dashboard" / "index.html"


def _client(**kwargs: str) -> TestClient:
    app = FastAPI()
    app.include_router(make_config_router(Settings(_env_file=None, **kwargs)))  # type: ignore[call-arg]
    return TestClient(app)


def test_get_shows_the_role_map_and_judge_without_any_key() -> None:
    with _client() as client:
        body = client.get("/api/config").json()

    assert body["primary_provider"] == "anthropic"
    assert body["roles"]["orchestrator"] == {
        "primary": "anthropic/claude-sonnet-4-6",
        "fallback": "openai/gpt-4o",
    }
    assert body["roles"]["triage"] == {
        "primary": "anthropic/claude-haiku-4-5",
        "fallback": "openai/gpt-4o-mini",
    }
    assert set(body["roles"]) == {
        "orchestrator",
        "triage",
        "analysis",
        "reflexion",
        "resolution",
        "reverification",
        "pr_content_generator",
    }
    assert body["judge_model"] == "openai/gpt-4o"


def test_get_follows_the_primary_provider() -> None:
    with _client(sentinel_primary_provider="openai") as client:
        body = client.get("/api/config").json()
    assert body["roles"]["analysis"] == {
        "primary": "openai/gpt-4o",
        "fallback": "anthropic/claude-sonnet-4-6",
    }
    assert body["judge_model"] == "anthropic/claude-sonnet-4-6"


def test_put_can_no_longer_repoint_models() -> None:
    """Decision 2026-10-15: the PUT /api/config model-repointing ability is removed."""
    with _client() as client:
        put = client.put("/api/config", json={"judge_model": "anthropic/claude-haiku-4-5"})
        body = client.get("/api/config").json()
    assert put.status_code == 405
    assert body["judge_model"] == "openai/gpt-4o"


def test_dashboard_no_longer_offers_model_switching() -> None:
    html = _DASHBOARD.read_text(encoding="utf-8")
    assert "method: 'PUT'" not in html
    assert "saveConfig" not in html
    assert "/api/config" in html  # the read-only badges remain
