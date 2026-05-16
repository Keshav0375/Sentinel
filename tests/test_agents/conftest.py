"""Shared fixtures for all agent tests."""

from __future__ import annotations

import pytest

from sentinel.config import Settings


@pytest.fixture()
def fake_settings() -> Settings:
    """Minimal Settings with a fake Groq key — no .env required."""
    return Settings(groq_api_key="gsk_test")
