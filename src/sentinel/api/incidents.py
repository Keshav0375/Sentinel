"""Incident query endpoints (backend.md §3.2) — ``GET /incidents``, ``/incidents/{id}``,
``/incidents/{id}/trajectory``, ``/incidents/{id}/deploy``, ``/incidents/{id}/rollback-spec``.

An incident is served from the ``incidents`` table when it has a row; otherwise, if
short-term memory still holds it, as an in-flight row with ``status: "in_progress"`` (its
live timeline in ``trajectory``, shaped like a stored one); otherwise 404. No database (or
a failed query) is 503 on every route. An unknown incident is 404, and so is a known one
with no linked ``deployments`` row on ``/deploy``. ``/rollback-spec`` is the RollbackSpec the
pipeline drafted, read from ``trajectory.pipeline_result.rollback_spec`` of a stored
``rollback_proposed`` row; every other case (in flight, escalated, no spec) is 404.

No auth here: the app mounts this router behind the Entra bearer dependency (task 5.5).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, JsonValue, ValidationError

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.logging import get_logger
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import IncidentAlert
from sentinel.models.incident import IncidentStatus, TimelineEntry
from sentinel.models.memory import DeploymentRow, IncidentRow
from sentinel.models.tools import RollbackSpec

logger = get_logger("sentinel.api.incidents")

IN_PROGRESS: Final = "in_progress"
_DEFAULT_LIMIT: Final = 50  # EpisodicMemory.list_incidents' default
_MAX_LIMIT: Final = 200


class IncidentListResponse(BaseModel):
    """GET /incidents: in-flight incidents first, then stored ones newest first."""

    incidents: list[IncidentRow]


class IncidentTrajectoryResponse(BaseModel):
    """GET /incidents/{id}/trajectory: the incident's trajectory JSONB (null when none)."""

    incident_id: str
    trajectory: JsonValue = None


def _in_flight_row(context: dict[str, Any]) -> IncidentRow:
    """An incident only short-term memory holds, as an IncidentRow with status in_progress."""
    incident_id: str = context["incident_id"]
    alert: IncidentAlert = context["alert"]
    timeline: list[TimelineEntry] = context["timeline"]
    trajectory: dict[str, JsonValue] = {
        "timeline": {
            "id": incident_id,
            "status": IN_PROGRESS,
            "timeline": [entry.model_dump(mode="json") for entry in timeline],
        }
    }
    if context["result"] is not None:
        trajectory["pipeline_result"] = context["result"]
    return IncidentRow(
        id=incident_id,
        alert_id=alert.alert_id,
        dd_event_id=alert.dd_event_id,
        severity=alert.severity,
        service=alert.service,
        status=IN_PROGRESS,
        trajectory=trajectory,
    )


def rollback_spec_of(row: IncidentRow) -> RollbackSpec | None:
    """The RollbackSpec a stored ``rollback_proposed`` row carries, else None.

    Read from ``trajectory.pipeline_result.rollback_spec``. An invalid spec, or one for
    another incident, is logged and treated as absent.
    """
    if row.status != IncidentStatus.ROLLBACK_PROPOSED.value:
        return None
    trajectory = row.trajectory
    if not isinstance(trajectory, dict):
        return None
    pipeline_result = trajectory.get("pipeline_result")
    if not isinstance(pipeline_result, dict):
        return None
    raw = pipeline_result.get("rollback_spec")
    if not isinstance(raw, dict):
        return None
    try:
        spec = RollbackSpec.model_validate(raw)
    except ValidationError:
        logger.warning("rollback_spec_invalid", incident_id=row.id)
        return None
    try:
        matches = uuid.UUID(spec.incident_id) == uuid.UUID(row.id)
    except ValueError:
        matches = False
    if not matches:
        logger.warning("rollback_spec_incident_mismatch", incident_id=row.id)
        return None
    return spec


def _unavailable(exc: SentinelMemoryError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


def _not_found(incident_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Incident {incident_id!r} not found.")


def make_incidents_router(
    short_term_memory: ShortTermMemory, episodic_memory: EpisodicMemory
) -> APIRouter:
    """Create the incident query router.

    Args:
        short_term_memory: Live ``ShortTermMemory`` holding the incidents in flight.
        episodic_memory: ``EpisodicMemory`` over the ``incidents`` and ``deployments``
            tables, the source of every stored incident.

    Returns:
        ``APIRouter`` with the five ``GET /incidents*`` routes registered under no prefix
        and no auth dependency (the caller mounts it at the root, behind auth).
    """
    router = APIRouter()

    async def _in_flight(service: str | None) -> list[IncidentRow]:
        ids = short_term_memory.active_incident_ids
        stored = await episodic_memory.stored_incident_ids(ids)
        rows: list[IncidentRow] = []
        for incident_id in ids:
            if incident_id in stored:
                continue
            try:
                context = short_term_memory.get_context(incident_id)
            except KeyError:
                continue  # cleared meanwhile
            row = _in_flight_row(context)
            if service is None or row.service == service:
                rows.append(row)
        return rows

    async def _find(incident_id: str) -> IncidentRow:
        row = await episodic_memory.get_incident_row(incident_id)
        if row is not None:
            return row
        try:
            context = short_term_memory.get_context(incident_id)
        except KeyError:
            raise _not_found(incident_id) from None
        return _in_flight_row(context)

    @router.get("/incidents", response_model=IncidentListResponse)
    async def list_incidents(  # pyright: ignore[reportUnusedFunction]
        status: str | None = None,
        service: str | None = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    ) -> IncidentListResponse:
        """List incidents: in-flight first, then stored newest first.

        ``status=in_progress`` lists only the in-flight ones; any other status only the
        stored ones; none lists both. ``service`` filters both. ``limit`` is 1-200
        (default 50).
        """
        try:
            in_flight = await _in_flight(service) if status in (None, IN_PROGRESS) else []
            if status == IN_PROGRESS:
                rows = in_flight[:limit]
            else:
                stored = await episodic_memory.list_incident_rows(
                    status=status, service=service, limit=limit
                )
                rows = (in_flight + stored)[:limit]
        except SentinelMemoryError as exc:
            raise _unavailable(exc) from exc
        return IncidentListResponse(incidents=rows)

    @router.get("/incidents/{incident_id}", response_model=IncidentRow)
    async def get_incident(  # pyright: ignore[reportUnusedFunction]
        incident_id: str,
    ) -> IncidentRow:
        """One incident: its stored row, else its in-flight state, else 404."""
        try:
            return await _find(incident_id)
        except SentinelMemoryError as exc:
            raise _unavailable(exc) from exc

    @router.get("/incidents/{incident_id}/trajectory", response_model=IncidentTrajectoryResponse)
    async def get_trajectory(  # pyright: ignore[reportUnusedFunction]
        incident_id: str,
    ) -> IncidentTrajectoryResponse:
        """The incident's trajectory JSONB (null when the row has none)."""
        try:
            row = await _find(incident_id)
        except SentinelMemoryError as exc:
            raise _unavailable(exc) from exc
        return IncidentTrajectoryResponse(incident_id=row.id, trajectory=row.trajectory)

    @router.get("/incidents/{incident_id}/deploy", response_model=DeploymentRow)
    async def get_deploy(  # pyright: ignore[reportUnusedFunction]
        incident_id: str,
    ) -> DeploymentRow:
        """The ``deployments`` row linked to the incident; 404 when unknown or unlinked."""
        try:
            await _find(incident_id)
            deploy = await episodic_memory.get_linked_deployment(incident_id)
        except SentinelMemoryError as exc:
            raise _unavailable(exc) from exc
        if deploy is None:
            raise HTTPException(
                status_code=404,
                detail=f"No deployment is linked to incident {incident_id!r}.",
            )
        return deploy

    @router.get("/incidents/{incident_id}/rollback-spec", response_model=RollbackSpec)
    async def get_rollback_spec(  # pyright: ignore[reportUnusedFunction]
        incident_id: str,
    ) -> RollbackSpec:
        """The RollbackSpec the pipeline drafted.

        404 when the incident is unknown, in flight, escalated or has no spec.
        """
        try:
            row = await episodic_memory.get_incident_row(incident_id)
        except SentinelMemoryError as exc:
            raise _unavailable(exc) from exc
        if row is None:
            raise _not_found(incident_id)
        spec = rollback_spec_of(row)
        if spec is None:
            raise HTTPException(
                status_code=404, detail=f"Incident {incident_id!r} has no rollback spec."
            )
        return spec

    return router
