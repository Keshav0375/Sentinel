"""Tests for sentinel.providers.capabilities."""

from __future__ import annotations

import pytest

from sentinel.providers.capabilities import ProviderCapabilities, get_capabilities

# ── Structured-output-capable providers ───────────────────────────────────────


def test_openai_capabilities() -> None:
    caps = get_capabilities("openai/gpt-4o")
    assert caps.supports_structured_outputs is True
    assert caps.strict_schemas is True


def test_azure_capabilities() -> None:
    caps = get_capabilities("azure/my-gpt4o-deployment")
    assert caps.supports_structured_outputs is True
    assert caps.strict_schemas is True


def test_groq_capabilities() -> None:
    caps = get_capabilities("groq/llama-3.3-70b-versatile")
    assert caps.supports_structured_outputs is False
    assert caps.strict_schemas is False


# ── Providers without structured outputs ──────────────────────────────────────


def test_anthropic_capabilities() -> None:
    caps = get_capabilities("anthropic/claude-sonnet-4-6")
    assert caps.supports_structured_outputs is False
    assert caps.strict_schemas is False


# ── Safe-default cases ────────────────────────────────────────────────────────


def test_unknown_provider_returns_safe_default() -> None:
    caps = get_capabilities("cohere/command-r")
    assert caps.supports_structured_outputs is False
    assert caps.strict_schemas is False


def test_bare_model_name_returns_safe_default() -> None:
    """Bare model strings without a provider prefix get the safe default."""
    caps = get_capabilities("llama-3.1-8b-instant")
    assert caps.supports_structured_outputs is False
    assert caps.strict_schemas is False


def test_empty_string_returns_safe_default() -> None:
    caps = get_capabilities("")
    assert caps.supports_structured_outputs is False
    assert caps.strict_schemas is False


# ── ProviderCapabilities is frozen ────────────────────────────────────────────


def test_capabilities_are_frozen() -> None:
    """ProviderCapabilities is a frozen dataclass — mutation must raise."""
    caps = get_capabilities("groq/llama-3.1-8b-instant")
    with pytest.raises((AttributeError, TypeError)):
        caps.supports_structured_outputs = True  # type: ignore[misc]


def test_return_type_is_provider_capabilities() -> None:
    result = get_capabilities("groq/llama-3.1-8b-instant")
    assert isinstance(result, ProviderCapabilities)


# ── Case sensitivity ──────────────────────────────────────────────────────────


def test_provider_prefix_is_case_insensitive() -> None:
    """Provider prefixes are lower-cased before lookup."""
    caps_lower = get_capabilities("groq/llama-3.1-8b-instant")
    caps_upper = get_capabilities("GROQ/llama-3.1-8b-instant")
    assert caps_lower == caps_upper
