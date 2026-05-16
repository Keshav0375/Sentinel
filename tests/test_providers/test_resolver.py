"""Tests for sentinel.providers.resolver."""

from __future__ import annotations

import os

import pytest
from agents.extensions.models.litellm_model import LitellmModel

from sentinel.config import Settings
from sentinel.providers.resolver import resolve_model


def _settings(**kwargs: str) -> Settings:
    """Create a minimal Settings instance with the given overrides."""
    return Settings(groq_api_key="gsk_test", **kwargs)  # type: ignore[arg-type]


# ── Supported providers — assert returns LitellmModel ────────────────────────


def test_resolve_groq_returns_litellm_model() -> None:
    settings = _settings()
    model = resolve_model("groq/llama-3.1-8b-instant", settings)
    assert isinstance(model, LitellmModel)


def test_resolve_openai_returns_litellm_model() -> None:
    settings = _settings(openai_api_key="sk-test")
    model = resolve_model("openai/gpt-4o", settings)
    assert isinstance(model, LitellmModel)


def test_resolve_anthropic_returns_litellm_model() -> None:
    settings = _settings(anthropic_api_key="sk-ant-test")
    model = resolve_model("anthropic/claude-sonnet-4-6", settings)
    assert isinstance(model, LitellmModel)


def test_resolve_azure_returns_litellm_model() -> None:
    settings = _settings(
        azure_api_key="azure-key",
        azure_api_base="https://myresource.openai.azure.com/",
    )
    model = resolve_model("azure/gpt-4o-deployment", settings)
    assert isinstance(model, LitellmModel)


# ── Azure sets environment variables ─────────────────────────────────────────


def test_resolve_azure_sets_api_base_env_var() -> None:
    base = "https://myresource.openai.azure.com/"
    settings = _settings(azure_api_key="azure-key", azure_api_base=base)
    resolve_model("azure/gpt-4o-deployment", settings)
    assert os.environ.get("AZURE_API_BASE") == base


def test_resolve_azure_sets_api_version_env_var() -> None:
    settings = _settings(
        azure_api_key="azure-key",
        azure_api_base="https://myresource.openai.azure.com/",
        azure_api_version="2024-06-01",
    )
    resolve_model("azure/gpt-4o-deployment", settings)
    assert os.environ.get("AZURE_API_VERSION") == "2024-06-01"


# ── Model string forwarded to LitellmModel ────────────────────────────────────


def test_resolve_groq_model_string_forwarded() -> None:
    settings = _settings()
    model = resolve_model("groq/llama-3.3-70b-versatile", settings)
    assert model.model == "groq/llama-3.3-70b-versatile"


def test_resolve_anthropic_model_string_forwarded() -> None:
    settings = _settings(anthropic_api_key="sk-ant-test")
    model = resolve_model("anthropic/claude-haiku-4-5", settings)
    assert model.model == "anthropic/claude-haiku-4-5"


# ── API key forwarded to LitellmModel ─────────────────────────────────────────


def test_resolve_groq_api_key_forwarded() -> None:
    settings = _settings()
    model = resolve_model("groq/llama-3.1-8b-instant", settings)
    assert model.api_key == "gsk_test"


def test_resolve_openai_api_key_forwarded() -> None:
    settings = _settings(openai_api_key="sk-openai-key")
    model = resolve_model("openai/gpt-4o-mini", settings)
    assert model.api_key == "sk-openai-key"


# ── Error cases ───────────────────────────────────────────────────────────────


def test_resolve_unknown_prefix_raises_value_error() -> None:
    settings = _settings()
    with pytest.raises(ValueError, match="Unknown provider"):
        resolve_model("cohere/command-r", settings)


def test_resolve_bare_model_name_raises_value_error() -> None:
    """Model strings without a '/' separator must raise ValueError."""
    settings = _settings()
    with pytest.raises(ValueError, match="provider/model"):
        resolve_model("llama-3.1-8b-instant", settings)


def test_resolve_empty_string_raises_value_error() -> None:
    settings = _settings()
    with pytest.raises(ValueError):
        resolve_model("", settings)


def test_resolve_unsupported_provider_error_lists_supported() -> None:
    settings = _settings()
    with pytest.raises(ValueError, match="groq"):
        resolve_model("mistral/mistral-large", settings)
