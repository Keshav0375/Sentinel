"""Scenarios API — GET /api/scenarios: the scenario-branch catalog, read-only.

Serves the 30 Sentinel-deployment scenario branches (decision 2026-10-25) from the vendored
``sentinel/catalog/branches.yaml``. A scenario runs only as a human-merged PR on
Sentinel-deployment; this route only lists them. main.py mounts it behind the bearer.
"""

from __future__ import annotations

from fastapi import APIRouter

from sentinel.catalog import load_catalog
from sentinel.eval.runner import BranchTruth


def make_scenarios_router() -> APIRouter:
    """Create the scenarios list router.

    Returns:
        ``APIRouter`` with ``GET /api/scenarios`` registered.
    """
    router = APIRouter()

    @router.get("/api/scenarios", response_model=list[BranchTruth])
    async def get_scenarios() -> list[BranchTruth]:  # pyright: ignore[reportUnusedFunction]
        """Every catalog entry, in branches.yaml order."""
        return list(load_catalog())

    return router
