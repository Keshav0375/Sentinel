"""Tests for sentinel.providers.capabilities."""

from __future__ import annotations

import pytest

from sentinel.providers.capabilities import (
    ProviderCapabilities,
    common_capabilities,
    get_capabilities,
)

# ── Structured-output-capable providers ───────────────────────────────────────


def test_openai_capabilities() -> None:
    caps = get_capabilities("openai/gpt-4o")
    assert caps.supports_structured_outputs is True
    assert caps.strict_schemas is True


@pytest.mark.parametrize("model", ["groq/llama-3.3-70b-versatile", "azure/gpt-4o-deployment"])
def test_removed_providers_get_the_safe_default(model: str) -> None:
    """§4.7 allows Anthropic and OpenAI only; any other prefix gets nothing."""
    caps = get_capabilities(model)
    assert caps.supports_structured_outputs is False
    assert caps.strict_schemas is False


def test_common_capabilities_is_what_both_support() -> None:
    """An agent may fall back to the other provider, so it gets the intersection."""
    both = common_capabilities("anthropic/claude-sonnet-4-6", "openai/gpt-4o")
    assert both == ProviderCapabilities(supports_structured_outputs=False, strict_schemas=False)
    only_openai = common_capabilities("openai/gpt-4o", "openai/gpt-4o-mini")
    assert only_openai == ProviderCapabilities(
        supports_structured_outputs=True, strict_schemas=True
    )


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
    caps = get_capabilities("openai/gpt-4o-mini")
    with pytest.raises((AttributeError, TypeError)):
        caps.supports_structured_outputs = True  # type: ignore[misc]


def test_return_type_is_provider_capabilities() -> None:
    result = get_capabilities("openai/gpt-4o-mini")
    assert isinstance(result, ProviderCapabilities)


# ── Case sensitivity ──────────────────────────────────────────────────────────


def test_provider_prefix_is_case_insensitive() -> None:
    """Provider prefixes are lower-cased before lookup."""
    caps_lower = get_capabilities("openai/gpt-4o-mini")
    caps_upper = get_capabilities("OPENAI/gpt-4o-mini")
    assert caps_lower == caps_upper
