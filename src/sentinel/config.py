"""Environment-variable-driven settings via pydantic-settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Maps provider prefix → (env_var_name, Settings field name) for error messages.
_PROVIDER_KEY_FIELDS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "azure": "AZURE_API_KEY",
}


class Settings(BaseSettings):
    """All runtime configuration loaded from environment variables / .env file.

    Intended to be instantiated once at startup and injected via FastAPI Depends
    or passed explicitly — never imported as a module-level singleton.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Groq (primary dev provider) ───────────────────────────────────────────
    # Required — no default. All model defaults use groq/ so this key is always
    # needed unless you override every model string to a different provider.
    groq_api_key: str

    # Base URL for the Groq-compatible OpenAI client.
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # ── OpenAI ────────────────────────────────────────────────────────────────
    # Required only when a sentinel_*_model field uses the "openai/" prefix.
    openai_api_key: str = ""

    # ── Anthropic ─────────────────────────────────────────────────────────────
    # Required only when a sentinel_*_model field uses the "anthropic/" prefix.
    anthropic_api_key: str = ""

    # ── Azure OpenAI ──────────────────────────────────────────────────────────
    # Required only when a sentinel_*_model field uses the "azure/" prefix.
    azure_api_key: str = ""
    azure_api_base: str = ""
    azure_api_version: str = "2024-02-01"

    # ── Gemini (future — excluded from provider layer for now) ────────────────
    gemini_api_key: str = ""

    # ── Models — provider/model format (LiteLLM) ──────────────────────────────
    # Each string selects both the provider and the model in one value.
    # Change the prefix to switch providers: groq/ openai/ anthropic/ azure/
    # See available_models.md for the full list of supported strings.

    # Fast model — triage classification and eval judging.
    sentinel_triage_model: str = "groq/llama-3.1-8b-instant"

    # Capable model — log analysis, deploy correlation, remediation drafting.
    sentinel_analysis_model: str = "groq/llama-3.3-70b-versatile"

    # Judge model — kept separate so it can be a different family than the agents.
    sentinel_judge_model: str = "groq/llama-3.1-8b-instant"

    # Local sentence-transformers model for zero-cost embedding (384 dims).
    sentinel_embedding_model: str = "all-MiniLM-L6-v2"

    # ── Infrastructure ────────────────────────────────────────────────────────
    sentinel_db_path: Path = Path("./data/sentinel.db")

    sentinel_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Hard cap on tool calls per incident to prevent runaway loops.
    sentinel_max_tool_calls: int = 15

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("sentinel_max_tool_calls")
    @classmethod
    def max_tool_calls_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("sentinel_max_tool_calls must be >= 1")
        return v

    @field_validator("groq_api_key")
    @classmethod
    def groq_api_key_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("groq_api_key must not be empty")
        return v

    @model_validator(mode="after")
    def validate_provider_keys(self) -> Settings:
        """Assert the API key for each active provider is set.

        Reads the provider prefix from each sentinel_*_model string and checks
        that the matching API key field is non-empty. Groq is always valid
        because groq_api_key is required. Bare model names (no '/' separator)
        are allowed through for backwards compatibility.
        """
        models_to_check = [
            ("SENTINEL_TRIAGE_MODEL", self.sentinel_triage_model),
            ("SENTINEL_ANALYSIS_MODEL", self.sentinel_analysis_model),
            ("SENTINEL_JUDGE_MODEL", self.sentinel_judge_model),
        ]

        for env_var, model_string in models_to_check:
            if "/" not in model_string:
                continue  # bare model name — no provider prefix to validate
            provider = model_string.split("/")[0].lower()

            if provider == "groq":
                pass  # groq_api_key is required and already validated above
            elif provider == "openai":
                if not self.openai_api_key.strip():
                    raise ValueError(
                        f"{env_var}={model_string!r} requires OPENAI_API_KEY to be set"
                    )
            elif provider == "anthropic":
                if not self.anthropic_api_key.strip():
                    raise ValueError(
                        f"{env_var}={model_string!r} requires ANTHROPIC_API_KEY to be set"
                    )
            elif provider == "azure":
                if not self.azure_api_key.strip():
                    raise ValueError(
                        f"{env_var}={model_string!r} requires AZURE_API_KEY to be set"
                    )
                if not self.azure_api_base.strip():
                    raise ValueError(
                        f"{env_var}={model_string!r} requires AZURE_API_BASE to be set"
                    )

        return self


def get_settings() -> Settings:
    """Create a Settings instance from the current environment.

    Call this once at app startup and pass the result via dependency injection.
    In tests, instantiate Settings directly with explicit kwargs to avoid
    requiring a .env file.
    """
    return Settings()  # type: ignore[call-arg]  # required fields populated from env
