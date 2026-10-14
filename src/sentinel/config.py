"""Environment-variable-driven settings via pydantic-settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Every credential field. All optional: no secret is required to boot, and a feature whose
# key is missing fails at use (backend.md §8.2; decision 2026-10-14). A blank value counts
# as missing.
_SECRET_FIELDS = (
    "groq_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "azure_api_key",
    "gemini_api_key",
    "dd_api_key",
    "dd_app_key",
    "github_api_token",
    "langfuse_public_key",
    "langfuse_secret_key",
)


class Settings(BaseSettings):
    """All runtime configuration loaded from environment variables / .env file.

    Intended to be instantiated once at startup and injected via FastAPI Depends
    or passed explicitly — never imported as a module-level singleton.

    No field is required: the app boots with no credentials at all. Secrets are
    ``SecretStr | None`` (masked in repr and logs); read one with ``secret_value``,
    which raises a clear error naming the env var when it is unset.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Groq (primary dev provider) ───────────────────────────────────────────
    # All model defaults use groq/, so running an incident needs this key, but booting
    # does not: resolve_model fails with a clear error when it is unset.
    groq_api_key: SecretStr | None = None

    # Base URL for the Groq-compatible OpenAI client.
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # ── OpenAI ────────────────────────────────────────────────────────────────
    # Required only when a sentinel_*_model field uses the "openai/" prefix.
    openai_api_key: SecretStr | None = None

    # ── Anthropic ─────────────────────────────────────────────────────────────
    # Required only when a sentinel_*_model field uses the "anthropic/" prefix.
    anthropic_api_key: SecretStr | None = None

    # ── Azure OpenAI ──────────────────────────────────────────────────────────
    # Required only when a sentinel_*_model field uses the "azure/" prefix.
    azure_api_key: SecretStr | None = None
    azure_api_base: str = ""
    azure_api_version: str = "2024-02-01"

    # ── Gemini (future — excluded from provider layer for now) ────────────────
    gemini_api_key: SecretStr | None = None

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

    # ── PostgreSQL + pgvector (Phase 2) ───────────────────────────────────────
    # asyncpg DSN (postgresql://…). Empty → no Postgres pool is created; the app still
    # boots and every memory call reports "memory unavailable" (decision 2026-10-12).
    database_url: str = ""

    # asyncpg pool bounds — one connection per concurrent incident (backend.md §4.5).
    sentinel_db_pool_min: int = 2
    sentinel_db_pool_max: int = 10

    sentinel_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Hard cap on tool calls per incident to prevent runaway loops (§4.6).
    sentinel_max_tool_calls: int = 20

    # Stub provider calls in tests/CI (§9.1); /ready skips the live-provider check.
    sentinel_fake_llm: bool = False

    # ── LangFuse (§6) ─────────────────────────────────────────────────────────
    # The langfuse v3 client reads LANGFUSE_* from the process environment itself
    # (infra/tracing.configure_langfuse); these fields mirror the same env vars.
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str | None = None

    # ── Datadog (fetch_logs — §4.8) ───────────────────────────────────────────
    # The app key is scoped to logs_read_data + synthetics_read (read-only).
    dd_api_key: SecretStr | None = None
    dd_app_key: SecretStr | None = None
    dd_site: str = "us5.datadoghq.com"

    # ── GitHub (get_deploy_details — §4.8) ────────────────────────────────────
    # Read-only fine-grained PAT on Keshav0375/Sentinel-deployment (Contents + PRs read).
    github_api_token: SecretStr | None = None

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("sentinel_max_tool_calls")
    @classmethod
    def max_tool_calls_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("sentinel_max_tool_calls must be >= 1")
        return v

    @model_validator(mode="after")
    def db_pool_bounds_valid(self) -> Settings:
        if self.sentinel_db_pool_min < 0:
            raise ValueError("sentinel_db_pool_min must be >= 0")
        if self.sentinel_db_pool_max < 1:
            raise ValueError("sentinel_db_pool_max must be >= 1")
        if self.sentinel_db_pool_min > self.sentinel_db_pool_max:
            raise ValueError("sentinel_db_pool_min must be <= sentinel_db_pool_max")
        return self

    @field_validator(*_SECRET_FIELDS, "langfuse_base_url", mode="before")
    @classmethod
    def blank_is_unset(cls, v: object) -> object:
        """A blank or whitespace-only value counts as missing (None)."""
        if isinstance(v, str) and not v.strip():
            return None
        return v


def get_settings() -> Settings:
    """Create a Settings instance from the current environment.

    Call this once at app startup and pass the result via dependency injection.
    In tests, instantiate Settings directly with explicit kwargs to avoid
    requiring a .env file.
    """
    return Settings()


def secret_value(secret: SecretStr | None, env_var: str) -> str:
    """Return *secret*'s plain value at the point of use.

    Raises:
        ValueError: The secret is unset; the message names *env_var* so the operator
            knows which key to provide. Callers turn this into their own failure.
    """
    if secret is None:
        raise ValueError(f"{env_var} is not set")
    return secret.get_secret_value()
