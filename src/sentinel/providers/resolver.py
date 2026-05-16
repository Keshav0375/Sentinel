"""Provider resolver — maps a provider/model string to a LitellmModel instance."""

from __future__ import annotations

import os

from agents.extensions.models.litellm_model import LitellmModel

from sentinel.config import Settings

# Maps provider prefix → Settings field name that holds the API key.
_PROVIDER_KEY_FIELDS: dict[str, str] = {
    "groq": "groq_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "azure": "azure_api_key",
}


def resolve_model(model_string: str, settings: Settings) -> LitellmModel:
    """Map a provider/model string to a configured LitellmModel.

    Parses the provider prefix, selects the matching API key from Settings,
    sets any provider-specific env vars (e.g. Azure endpoint), and returns
    a LitellmModel ready to be passed to an Agent's model= parameter.

    Args:
        model_string: A 'provider/model' string in LiteLLM format, e.g.:
            - 'groq/llama-3.1-8b-instant'
            - 'openai/gpt-4o'
            - 'anthropic/claude-sonnet-4-6'
            - 'azure/my-gpt4o-deployment'
        settings: Fully-validated Settings instance. The model validator in
            config.py ensures the matching API key is non-empty before this
            function is called.

    Returns:
        LitellmModel configured with the correct API key for the provider.

    Raises:
        ValueError: If model_string has no '/' separator or if the provider
            prefix is not one of the four supported providers.
    """
    if "/" not in model_string:
        raise ValueError(
            f"Model string {model_string!r} must use 'provider/model' format "
            f"(e.g. 'groq/llama-3.1-8b-instant'). "
            f"Supported prefixes: {sorted(_PROVIDER_KEY_FIELDS)}"
        )

    provider = model_string.split("/")[0].lower()

    if provider not in _PROVIDER_KEY_FIELDS:
        raise ValueError(
            f"Unknown provider prefix {provider!r} in {model_string!r}. "
            f"Supported providers: {sorted(_PROVIDER_KEY_FIELDS)}"
        )

    api_key: str = getattr(settings, _PROVIDER_KEY_FIELDS[provider])

    # Azure requires endpoint + version in env for LiteLLM to route correctly.
    if provider == "azure":
        os.environ["AZURE_API_BASE"] = settings.azure_api_base
        os.environ["AZURE_API_VERSION"] = settings.azure_api_version

    return LitellmModel(model=model_string, api_key=api_key)
