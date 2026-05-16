"""Tests for sentinel.config — Settings validation and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.config import Settings


def test_defaults_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prevent .env from overriding defaults during the test
    monkeypatch.delenv("SENTINEL_TRIAGE_MODEL", raising=False)
    monkeypatch.delenv("SENTINEL_ANALYSIS_MODEL", raising=False)
    monkeypatch.delenv("SENTINEL_JUDGE_MODEL", raising=False)
    s = Settings(groq_api_key="gsk_test", _env_file=None)  # type: ignore[call-arg]
    # Model defaults now use provider/model format
    assert s.sentinel_triage_model == "groq/llama-3.1-8b-instant"
    assert s.sentinel_analysis_model == "groq/llama-3.3-70b-versatile"
    assert s.sentinel_judge_model == "groq/llama-3.1-8b-instant"
    assert s.sentinel_embedding_model == "all-MiniLM-L6-v2"
    assert s.sentinel_db_path == Path("./data/sentinel.db")
    assert s.sentinel_log_level == "INFO"
    assert s.sentinel_max_tool_calls == 15
    assert s.groq_base_url == "https://api.groq.com/openai/v1"
    assert s.azure_api_version == "2024-02-01"
    assert isinstance(s.openai_api_key, str)
    assert isinstance(s.anthropic_api_key, str)
    assert isinstance(s.azure_api_key, str)
    assert isinstance(s.azure_api_base, str)


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
