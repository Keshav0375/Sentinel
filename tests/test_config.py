"""Tests for sentinel.config — Settings validation and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sentinel.config import Settings


def test_overrides_applied() -> None:
    s = Settings(
        groq_api_key="gsk_test",
        sentinel_triage_model="groq/llama-3.3-70b-versatile",
        sentinel_max_tool_calls=10,
        sentinel_log_level="DEBUG",
        sentinel_db_path=Path("/tmp/test.db"),
    )
    assert s.sentinel_triage_model == "groq/llama-3.3-70b-versatile"
    assert s.sentinel_max_tool_calls == 10
    assert s.sentinel_log_level == "DEBUG"
    assert s.sentinel_db_path == Path("/tmp/test.db")


def test_missing_groq_api_key_raises() -> None:
    with pytest.raises(Exception):
        Settings(groq_api_key="")  # type: ignore[call-arg]


def test_max_tool_calls_zero_raises() -> None:
    with pytest.raises(Exception):
        Settings(groq_api_key="gsk_test", sentinel_max_tool_calls=0)


def test_invalid_log_level_raises() -> None:
    with pytest.raises(Exception):
        Settings(groq_api_key="gsk_test", sentinel_log_level="VERBOSE")  # type: ignore[arg-type]


# ── Provider key validator tests ──────────────────────────────────────────────


def test_anthropic_model_without_key_raises() -> None:
    """anthropic/ prefix requires ANTHROPIC_API_KEY — missing key must raise."""
    with pytest.raises(Exception, match="ANTHROPIC_API_KEY"):
        Settings(
            groq_api_key="gsk_test",
            sentinel_analysis_model="anthropic/claude-sonnet-4-6",
            anthropic_api_key="",  # explicitly empty to override any env var
        )


def test_anthropic_model_with_key_passes() -> None:
    """anthropic/ prefix with a non-empty key must construct successfully."""
    s = Settings(
        groq_api_key="gsk_test",
        sentinel_analysis_model="anthropic/claude-sonnet-4-6",
        anthropic_api_key="sk-ant-test",
    )
    assert s.sentinel_analysis_model == "anthropic/claude-sonnet-4-6"
    assert s.anthropic_api_key == "sk-ant-test"


def test_openai_model_without_key_raises() -> None:
    """openai/ prefix requires OPENAI_API_KEY — missing key must raise."""
    with pytest.raises(Exception, match="OPENAI_API_KEY"):
        Settings(
            groq_api_key="gsk_test",
            sentinel_triage_model="openai/gpt-4o-mini",
            openai_api_key="",  # explicitly empty to override any env var
        )


def test_openai_model_with_key_passes() -> None:
    """openai/ prefix with a non-empty key must construct successfully."""
    s = Settings(
        groq_api_key="gsk_test",
        sentinel_triage_model="openai/gpt-4o-mini",
        openai_api_key="sk-test",
    )
    assert s.sentinel_triage_model == "openai/gpt-4o-mini"


def test_azure_model_missing_api_key_raises() -> None:
    """azure/ prefix requires AZURE_API_KEY."""
    with pytest.raises(Exception, match="AZURE_API_KEY"):
        Settings(
            groq_api_key="gsk_test",
            sentinel_judge_model="azure/gpt-4o-deployment",
            azure_api_key="",
            azure_api_base="https://myresource.openai.azure.com/",
        )


def test_azure_model_missing_api_base_raises() -> None:
    """azure/ prefix requires AZURE_API_BASE in addition to the key."""
    with pytest.raises(Exception, match="AZURE_API_BASE"):
        Settings(
            groq_api_key="gsk_test",
            sentinel_judge_model="azure/gpt-4o-deployment",
            azure_api_key="azure-key-test",
            azure_api_base="",
        )


def test_azure_model_full_config_passes() -> None:
    """azure/ prefix with both key and base URL must construct successfully."""
    s = Settings(
        groq_api_key="gsk_test",
        sentinel_judge_model="azure/gpt-4o-deployment",
        azure_api_key="azure-key-test",
        azure_api_base="https://myresource.openai.azure.com/",
    )
    assert s.sentinel_judge_model == "azure/gpt-4o-deployment"


def test_bare_model_name_passes() -> None:
    """Model strings without a '/' prefix are allowed (backwards compat)."""
    s = Settings(
        groq_api_key="gsk_test",
        sentinel_triage_model="llama-3.1-8b-instant",
        sentinel_analysis_model="llama-3.3-70b-versatile",
        sentinel_judge_model="llama-3.1-8b-instant",
    )
    assert s.sentinel_triage_model == "llama-3.1-8b-instant"


def test_groq_model_always_valid() -> None:
    """groq/ prefix is valid as long as groq_api_key is non-empty."""
    s = Settings(
        groq_api_key="gsk_test",
        sentinel_triage_model="groq/llama-3.1-8b-instant",
        sentinel_analysis_model="groq/llama-3.3-70b-versatile",
        sentinel_judge_model="groq/llama-3.1-8b-instant",
    )
    assert s.sentinel_triage_model == "groq/llama-3.1-8b-instant"


# ── PostgreSQL pool settings ──────────────────────────────────────────────────


def test_db_pool_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DATABASE_URL", "SENTINEL_DB_POOL_MIN", "SENTINEL_DB_POOL_MAX"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(groq_api_key="gsk_test", _env_file=None)  # type: ignore[call-arg]
    assert s.database_url == ""
    assert (s.sentinel_db_pool_min, s.sentinel_db_pool_max) == (2, 10)


def test_db_pool_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/sentinel")
    monkeypatch.setenv("SENTINEL_DB_POOL_MIN", "1")
    monkeypatch.setenv("SENTINEL_DB_POOL_MAX", "4")
    s = Settings(groq_api_key="gsk_test")
    assert s.database_url == "postgresql://u:p@localhost:5432/sentinel"
    assert (s.sentinel_db_pool_min, s.sentinel_db_pool_max) == (1, 4)


@pytest.mark.parametrize(
    ("pool_min", "pool_max", "message"),
    [
        (-1, 10, "sentinel_db_pool_min must be >= 0"),
        (0, 0, "sentinel_db_pool_max must be >= 1"),
        (5, 2, "sentinel_db_pool_min must be <= sentinel_db_pool_max"),
    ],
)
def test_db_pool_invalid_bounds_raise(pool_min: int, pool_max: int, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(
            groq_api_key="gsk_test",
            sentinel_db_pool_min=pool_min,
            sentinel_db_pool_max=pool_max,
        )
