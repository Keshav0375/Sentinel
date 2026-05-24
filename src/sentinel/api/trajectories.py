"""Trajectory history endpoint --- GET /api/trajectories.

Returns the list of saved incident trajectory files from ``reports/trajectories/``
so the dashboard can render a history sidebar of past pipeline runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel


class TrajectoryItem(BaseModel):
    """Single trajectory entry returned by GET /api/trajectories."""

    incident_id: str
    saved_at: str
    filename: str


def make_trajectories_router(
    trajectories_dir: Path = Path("reports/trajectories"),
) -> APIRouter:
    """Create the trajectory history router.

    Args:
        trajectories_dir: Directory containing trajectory JSON files.

    Returns:
        ``APIRouter`` with ``GET /api/trajectories`` registered.
    """
    router = APIRouter()

    @router.get("/api/trajectories", response_model=list[TrajectoryItem])
    async def list_trajectories() -> list[TrajectoryItem]:  # pyright: ignore[reportUnusedFunction]
        """Return saved trajectory files sorted by most recent first."""
        if not trajectories_dir.exists():
            return []
        items: list[TrajectoryItem] = []
        files = sorted(
            trajectories_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for f in files[:50]:
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                items.append(
                    TrajectoryItem(
                        incident_id=raw.get("incident_id", f.stem),
                        saved_at=raw.get("saved_at", ""),
                        filename=f.name,
                    )
                )
            except (json.JSONDecodeError, OSError):
                continue
        return items

    return router
