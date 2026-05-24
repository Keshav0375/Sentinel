"""Runtime model configuration --- GET/PUT /api/config.

Allows the dashboard to display and change which LLM models are used for each
agent role. Changes take effect on the next pipeline run (agents are rebuilt
per-incident so no restart is needed).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sentinel.config import Settings


class ModelConfig(BaseModel):
    """Response body for GET /api/config."""

    triage_model: str
    analysis_model: str
    judge_model: str
    available_providers: list[str]


class ModelUpdate(BaseModel):
    """Request body for PUT /api/config."""

    triage_model: str | None = None
    analysis_model: str | None = None
    judge_model: str | None = None


class RuntimeConfig:
    """Mutable model configuration that wraps base Settings.

    Overrides take effect on the next pipeline run. The base Settings
    (from .env) are used as defaults for any unset override.
    """

    def __init__(self, base: Settings) -> None:
        self._base = base
        self._overrides: dict[str, str] = {}

    @property
    def triage_model(self) -> str:
        return self._overrides.get("triage_model", self._base.sentinel_triage_model)

    @property
    def analysis_model(self) -> str:
        return self._overrides.get("analysis_model", self._base.sentinel_analysis_model)

    @property
    def judge_model(self) -> str:
        return self._overrides.get("judge_model", self._base.sentinel_judge_model)

    def update(self, **kwargs: str | None) -> None:
        for key, value in kwargs.items():
            if value:
                self._overrides[key] = value
            else:
                self._overrides.pop(key, None)

    def effective_settings(self) -> Settings:
        """Return Settings with any active overrides applied."""
        if not self._overrides:
            return self._base
        updates: dict[str, Any] = {}
        if "triage_model" in self._overrides:
            updates["sentinel_triage_model"] = self._overrides["triage_model"]
        if "analysis_model" in self._overrides:
            updates["sentinel_analysis_model"] = self._overrides["analysis_model"]
        if "judge_model" in self._overrides:
            updates["sentinel_judge_model"] = self._overrides["judge_model"]
        return self._base.model_copy(update=updates)

    @property
    def base_settings(self) -> Settings:
        """Return the underlying base Settings (read-only access for validation)."""
        return self._base

    def reset(self) -> None:
        """Clear all overrides, reverting to base settings."""
        self._overrides.clear()


_AVAILABLE_PROVIDERS = ["groq", "openai", "anthropic", "azure"]

_PROVIDER_KEY_MAP: dict[str, str] = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "azure": "azure_api_key",
}


def _check_provider_key(model_string: str, base: Settings) -> str | None:
    """Return an error message if the API key for this provider is missing."""
    if "/" not in model_string:
        return None
    provider = model_string.split("/")[0].lower()
    if provider == "groq":
        return None
    field = _PROVIDER_KEY_MAP.get(provider)
    if field is None:
        return None
    value = getattr(base, field, "")
    if not value:
        return f"{field.upper()} is required for model '{model_string}'"
    return None


def make_config_router(runtime_config: RuntimeConfig) -> APIRouter:
    """Create the configuration display/update router.

    Args:
        runtime_config: Mutable RuntimeConfig holding model overrides.

    Returns:
        ``APIRouter`` with GET and PUT ``/api/config`` registered.
    """
    router = APIRouter()

    @router.get("/api/config", response_model=ModelConfig)
    async def get_config() -> ModelConfig:  # pyright: ignore[reportUnusedFunction]
        """Return the current model configuration."""
        return ModelConfig(
            triage_model=runtime_config.triage_model,
            analysis_model=runtime_config.analysis_model,
            judge_model=runtime_config.judge_model,
            available_providers=_AVAILABLE_PROVIDERS,
        )

    @router.put("/api/config", response_model=ModelConfig)
    async def update_config(body: ModelUpdate) -> ModelConfig:  # pyright: ignore[reportUnusedFunction]
        """Update model configuration for subsequent pipeline runs."""
        errors: list[str] = []
        base = runtime_config.base_settings
        for _, model_val in [
            ("triage_model", body.triage_model),
            ("analysis_model", body.analysis_model),
            ("judge_model", body.judge_model),
        ]:
            if model_val:
                err = _check_provider_key(model_val, base)
                if err:
                    errors.append(err)
        if errors:
            raise HTTPException(status_code=422, detail="; ".join(errors))

        runtime_config.update(
            triage_model=body.triage_model,
            analysis_model=body.analysis_model,
            judge_model=body.judge_model,
        )
        return ModelConfig(
            triage_model=runtime_config.triage_model,
            analysis_model=runtime_config.analysis_model,
            judge_model=runtime_config.judge_model,
            available_providers=_AVAILABLE_PROVIDERS,
        )

    return router
