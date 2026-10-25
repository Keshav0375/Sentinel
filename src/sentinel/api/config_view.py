"""Model configuration view --- GET /api/config (read-only).

Shows the dashboard which model each agent role runs on. The models are fixed per role
(§4.7 table, ``providers/router.py``) and selected only by ``SENTINEL_PRIMARY_PROVIDER``;
nothing here can change them (decision 2026-10-15 removed the ``PUT /api/config``
model-repointing ability).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from sentinel.config import Settings
from sentinel.providers.router import ROLES, judge_model_string, role_models


class RoleModels(BaseModel):
    """The model pair one agent role runs on."""

    primary: str
    fallback: str


class ModelConfig(BaseModel):
    """Response body for GET /api/config."""

    primary_provider: str
    roles: dict[str, RoleModels]
    judge_model: str


def model_config_view(settings: Settings) -> ModelConfig:
    """The effective §4.7 model assignment under *settings*."""
    primary = settings.sentinel_primary_provider
    roles: dict[str, RoleModels] = {}
    for role in ROLES:
        primary_model, fallback_model = role_models(role, primary)
        roles[role] = RoleModels(primary=primary_model, fallback=fallback_model)
    return ModelConfig(
        primary_provider=primary,
        roles=roles,
        judge_model=judge_model_string(primary),
    )


def make_config_router(settings: Settings) -> APIRouter:
    """Create the read-only configuration router.

    Args:
        settings: The app's Settings; only ``sentinel_primary_provider`` is read.

    Returns:
        ``APIRouter`` with GET ``/api/config`` registered.
    """
    router = APIRouter()
    view = model_config_view(settings)

    @router.get("/api/config", response_model=ModelConfig)
    async def get_config() -> ModelConfig:  # pyright: ignore[reportUnusedFunction]
        """Return the model each agent role runs on."""
        return view

    return router
