"""Environment-variable-driven settings via pydantic-settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ── LLM providers ─────────────────────────────────────────────────────────
    # Primary: Groq (OpenAI-compatible). The openai client is pointed at Groq's
    # base URL with this key — no OPENAI_API_KEY needed during dev.
    groq_api_key: str

    # Backup / fallback for tasks that benefit from Gemini 2.5 Flash.
    gemini_api_key: str = ""

    # Base URL used when constructing the openai.AsyncOpenAI client for Groq.
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # ── Model selection ────────────────────────────────────────────────────────
    # Fast model for triage classification and eval judging.
    sentinel_triage_model: str = "llama-3.1-8b-instant"

    # Capable model for log analysis, deploy correlation, and remediation.
    sentinel_analysis_model: str = "llama-3.3-70b-versatile"

    # Judge model — kept separate so it can be swapped to a different family.
    sentinel_judge_model: str = "llama-3.1-8b-instant"

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


def get_settings() -> Settings:
    """Create a Settings instance from the current environment.

    Call this once at app startup and pass the result via dependency injection.
    In tests, instantiate Settings directly with explicit kwargs to avoid
    requiring a .env file.
    """
    return Settings()  # type: ignore[call-arg]  # required fields populated from env
