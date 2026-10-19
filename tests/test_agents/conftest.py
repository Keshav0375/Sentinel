"""Shared fixtures for all agent tests."""

from __future__ import annotations

import pytest

from sentinel.config import Settings


@pytest.fixture()
def fake_settings() -> Settings:
    """Minimal Settings with fake Anthropic and OpenAI keys — no .env required."""
    return Settings(anthropic_api_key="sk-ant-test", openai_api_key="sk-test")
