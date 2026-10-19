"""Provider capability flags — what each LLM backend supports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    """Feature flags for a provider derived from the model string prefix.

    Attributes:
        supports_structured_outputs: Whether the provider supports JSON-mode
            structured outputs (output_type= on Agent). Providers that don't
            support this need the output_coercion fallback.
        strict_schemas: Whether tool schemas can use strict/required validation.
            Providers with strict_schemas=False need strict_mode=False on tools.
    """

    supports_structured_outputs: bool
    strict_schemas: bool


# Per-provider capability table keyed by the provider prefix string (§4.7: Anthropic and
# OpenAI only).
_CAPABILITY_TABLE: dict[str, ProviderCapabilities] = {
    "openai": ProviderCapabilities(supports_structured_outputs=True, strict_schemas=True),
    "anthropic": ProviderCapabilities(supports_structured_outputs=False, strict_schemas=False),
}

# Used for unknown providers and bare model names — conservatively disable both.
_SAFE_DEFAULT = ProviderCapabilities(supports_structured_outputs=False, strict_schemas=False)


def common_capabilities(*model_strings: str) -> ProviderCapabilities:
    """The capabilities every one of *model_strings* supports.

    An agent is built once and may run on either model of its role pair (primary, then
    the fallback after a provider error), so it may only use a feature both support.
    """
    caps = [get_capabilities(m) for m in model_strings]
    return ProviderCapabilities(
        supports_structured_outputs=all(c.supports_structured_outputs for c in caps),
        strict_schemas=all(c.strict_schemas for c in caps),
    )


def get_capabilities(model_string: str) -> ProviderCapabilities:
    """Return capability flags for the provider identified by model_string.

    Reads the provider prefix (the part before the first '/') and looks it up
    in the capability table. Bare model names (no '/' separator) and unknown
    providers both return the safe default (both flags False).

    Args:
        model_string: A 'provider/model' string (e.g. 'anthropic/claude-haiku-4-5').
            Bare model names are accepted and return the safe default.

    Returns:
        ProviderCapabilities with feature flags for that provider.

    Examples:
        >>> get_capabilities("openai/gpt-4o")
        ProviderCapabilities(supports_structured_outputs=True, strict_schemas=True)
        >>> get_capabilities("anthropic/claude-sonnet-4-6")
        ProviderCapabilities(supports_structured_outputs=False, strict_schemas=False)
        >>> get_capabilities("unknown/model")
        ProviderCapabilities(supports_structured_outputs=False, strict_schemas=False)
    """
    if "/" not in model_string:
        return _SAFE_DEFAULT
    provider = model_string.split("/")[0].lower()
    return _CAPABILITY_TABLE.get(provider, _SAFE_DEFAULT)
