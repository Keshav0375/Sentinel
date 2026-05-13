"""Health check endpoint — GET /health."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from sentinel.memory.short_term import ShortTermMemory

_VERSION = "0.1.0"


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    status: Literal["ok"]
    version: str
    active_incidents: int


def make_health_router(short_term_memory: ShortTermMemory | None = None) -> APIRouter:
    """Create the health-check router.

    Args:
        short_term_memory: Optional STM instance used to report the number of
            active incidents. When ``None``, ``active_incidents`` is always 0
            (acceptable for early boot or testing without full DI setup).

    Returns:
        ``APIRouter`` with ``GET /health`` registered.
    """
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:  # pyright: ignore[reportUnusedFunction]
        """Return service health and the count of active incidents."""
        return HealthResponse(
            status="ok",
            version=_VERSION,
            active_incidents=short_term_memory.active_count if short_term_memory else 0,
        )

    return router
