"""Scenarios API — GET /api/scenarios.

Returns the list of all available scenarios with their alert payloads so the
dashboard can populate its "Fire Scenario" dropdown and POST the correct
``IncidentPayload`` (built from the scenario's alert) to ``/webhooks/incident``.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from sentinel.generator.alert_gen import generate_alert, list_scenarios, load_scenario
from sentinel.models.alert import AlertPayload


class ScenarioInfo(BaseModel):
    """Single scenario entry returned by GET /api/scenarios."""

    id: str
    label: str
    failure_class: str
    alert: AlertPayload


def make_scenarios_router() -> APIRouter:
    """Create the scenarios list router.

    Returns:
        ``APIRouter`` with ``GET /api/scenarios`` registered.
    """
    router = APIRouter()

    @router.get("/api/scenarios", response_model=list[ScenarioInfo])
    async def get_scenarios() -> list[ScenarioInfo]:  # pyright: ignore[reportUnusedFunction]
        """Return all available scenarios with pre-generated alert payloads.

        The dashboard builds an ``IncidentPayload`` from each alert for
        ``/webhooks/incident`` (with a fresh ``correlation_id`` per fire, so the
        deduplicator treats each fire as a new incident). Each call generates fresh
        ``alert_id`` / ``timestamp`` values.
        """
        results: list[ScenarioInfo] = []
        for sid in list_scenarios():
            scenario = load_scenario(sid)
            alert = generate_alert(scenario)
            label = f"{sid.replace('_', ' ').title()} — {scenario.description[:60]}"
            results.append(
                ScenarioInfo(
                    id=sid,
                    label=label,
                    failure_class=scenario.failure_class,
                    alert=alert,
                )
            )
        return results

    return router
