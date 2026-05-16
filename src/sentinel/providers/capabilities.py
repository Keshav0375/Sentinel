"""Provider capability flags — what each LLM backend supports."""

from __future__ import annotations

from dataclasses import dataclass

from agents import ModelSettings


@dataclass(frozen=True)
class ProviderCapabilities:
    """Feature flags for a provider derived from the model string prefix.

    Attributes:
        supports_structured_outputs: Whether the provider supports JSON-mode
            structured outputs (output_type= on Agent). Providers that don't
            support this need the output_coercion fallback.
        strict_schemas: Whether tool schemas can use strict/required validation.
            Providers with strict_schemas=False need strict_mode=False on tools.
        default_model_settings: Recommended ModelSettings for agents using
            this provider (e.g. temperature=0 for Groq tool reliability).
    """

    supports_structured_outputs: bool
    strict_schemas: bool
    default_model_settings: ModelSettings | None = None


# Groq Llama models need temperature=0 to reliably generate JSON tool calls
# instead of occasionally emitting XML-format calls that Groq's API rejects.
# num_retries handles the free-tier 6K/12K TPM rate limits via LiteLLM backoff.
_GROQ_SETTINGS = ModelSettings(temperature=0, extra_args={"num_retries": 5})

# Per-provider capability table keyed by the provider prefix string.
_CAPABILITY_TABLE: dict[str, ProviderCapabilities] = {
    "openai": ProviderCapabilities(supports_structured_outputs=True, strict_schemas=True),
    "azure": ProviderCapabilities(supports_structured_outputs=True, strict_schemas=True),
    "anthropic": ProviderCapabilities(supports_structured_outputs=False, strict_schemas=False),
    "groq": ProviderCapabilities(
        supports_structured_outputs=False,
        strict_schemas=False,
        default_model_settings=_GROQ_SETTINGS,
    ),
}

# Used for unknown providers and bare model names — conservatively disable both.
_SAFE_DEFAULT = ProviderCapabilities(supports_structured_outputs=False, strict_schemas=False)


def get_capabilities(model_string: str) -> ProviderCapabilities:
    """Return capability flags for the provider identified by model_string.

    Reads the provider prefix (the part before the first '/') and looks it up
    in the capability table. Bare model names (no '/' separator) and unknown
    providers both return the safe default (both flags False).

    Args:
        model_string: A 'provider/model' string (e.g. 'groq/llama-3.1-8b-instant').
            Bare model names are accepted and return the safe default.

    Returns:
        ProviderCapabilities with feature flags for that provider.

    Examples:
        >>> get_capabilities("groq/llama-3.3-70b-versatile")
        ProviderCapabilities(supports_structured_outputs=False, strict_schemas=False)
        >>> get_capabilities("anthropic/claude-sonnet-4-6")
        ProviderCapabilities(supports_structured_outputs=False, strict_schemas=False)
        >>> get_capabilities("unknown/model")
        ProviderCapabilities(supports_structured_outputs=False, strict_schemas=False)
    """
    if "/" not in model_string:
        return _SAFE_DEFAULT
    provider = model_string.split("/")[0].lower()
    return _CAPABILITY_TABLE.get(provider, _SAFE_DEFAULT)
