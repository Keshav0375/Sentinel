"""Tests for sentinel.config — Settings validation and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.config import Settings


def test_defaults_applied() -> None:
    # API key fields are excluded — they may be populated from the real .env,
    # which pydantic-settings reads at construction time.
    s = Settings(groq_api_key="gsk_test")
    assert s.sentinel_triage_model == "llama-3.1-8b-instant"
    assert s.sentinel_analysis_model == "llama-3.3-70b-versatile"
    assert s.sentinel_judge_model == "llama-3.1-8b-instant"
    assert s.sentinel_embedding_model == "all-MiniLM-L6-v2"
    assert s.sentinel_db_path == Path("./data/sentinel.db")
    assert s.sentinel_log_level == "INFO"
    assert s.sentinel_max_tool_calls == 15
    assert s.groq_base_url == "https://api.groq.com/openai/v1"
    assert isinstance(s.gemini_api_key, str)


def test_overrides_applied() -> None:
    s = Settings(
        groq_api_key="gsk_test",
        sentinel_triage_model="llama-3.3-70b-versatile",
        sentinel_max_tool_calls=10,
        sentinel_log_level="DEBUG",
        sentinel_db_path=Path("/tmp/test.db"),
    )
    assert s.sentinel_triage_model == "llama-3.3-70b-versatile"
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
