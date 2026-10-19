"""Incident query endpoints — GET /incidents and GET /incidents/{id}."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload
from sentinel.models.incident import TimelineEntry


class IncidentListItem(BaseModel):
    """Summary row returned in the GET /incidents list."""

    incident_id: str
    service: str
    status: str
    alert_severity: str


class IncidentListResponse(BaseModel):
    """Response body for GET /incidents."""

    incidents: list[IncidentListItem]
    total: int


class IncidentDetail(BaseModel):
    """Full incident context returned by GET /incidents/{id}."""

    incident_id: str
    alert: AlertPayload
    status: str
    timeline: list[TimelineEntry]
    # The PipelineResult once the pipeline has finished (rollback_proposed | escalated).
    result: dict[str, Any] | None


def make_incidents_router(short_term_memory: ShortTermMemory) -> APIRouter:
    """Create the incident query router.

    Args:
        short_term_memory: Live ``ShortTermMemory`` that holds all active
            incidents created by the webhook receiver.

    Returns:
        ``APIRouter`` with ``GET /incidents`` and ``GET /incidents/{id}``
        registered under no prefix (caller mounts at the root).
    """
    router = APIRouter()

    @router.get("/incidents", response_model=IncidentListResponse)
    async def list_incidents() -> IncidentListResponse:  # pyright: ignore[reportUnusedFunction]
        """List all currently active incidents (held in short-term memory).

        Returns an empty list when no incidents are in progress.
        """
        items: list[IncidentListItem] = []
        for inc_id in short_term_memory.active_incident_ids:
            ctx = short_term_memory.get_context(inc_id)
            alert: AlertPayload = ctx["alert"]
            items.append(
                IncidentListItem(
                    incident_id=inc_id,
                    service=alert.service,
                    status=str(ctx["status"]),
                    alert_severity=str(alert.severity),
                )
            )
        return IncidentListResponse(incidents=items, total=len(items))

    @router.get("/incidents/{incident_id}", response_model=IncidentDetail)
    async def get_incident(  # pyright: ignore[reportUnusedFunction]
        incident_id: str,
    ) -> IncidentDetail:
        """Return the full context for a single incident.

        Includes the original alert, every timeline step logged so far, and the
        pipeline result (null until the pipeline has finished).

        Raises 404 if the incident_id is not in short-term memory.
        """
        try:
            ctx = short_term_memory.get_context(incident_id)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"Incident {incident_id!r} not found in active memory.",
            )

        return IncidentDetail(
            incident_id=ctx["incident_id"],
            alert=ctx["alert"],
            status=str(ctx["status"]),
            timeline=ctx["timeline"],
            result=ctx["result"],
        )

    return router
