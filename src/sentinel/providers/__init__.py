"""Provider abstraction layer — model resolution, capability flags, SDK defaults.

Public API:
    resolve_model(model_string, settings) → LitellmModel
    get_capabilities(model_string) → ProviderCapabilities
    apply_sdk_defaults(settings) → None
    coerce_output(text, schema) → BaseModel | None
"""

from __future__ import annotations

from agents import set_default_openai_api, set_tracing_export_api_key

from sentinel.config import Settings
from sentinel.providers.capabilities import ProviderCapabilities, get_capabilities
from sentinel.providers.output_coercion import coerce_output
from sentinel.providers.resolver import LitellmModel, resolve_model

__all__ = [
    "ProviderCapabilities",
    "LitellmModel",
    "apply_sdk_defaults",
    "coerce_output",
    "get_capabilities",
    "resolve_model",
]


def apply_sdk_defaults(settings: Settings) -> None:
    """Configure the OpenAI Agents SDK for the active provider.

    Must be called once at application startup, before any agents are built.
    Centralises the two SDK-global calls that were previously scattered across
    main.py, run_scenario.py, and eval/runner.py.

    - Forces chat-completions API mode (required for Groq and most providers —
      the default 'responses' API is OpenAI-only).
    - Sets a blank tracing export key when no OpenAI key is configured so the
      SDK won't try to export traces to OpenAI's servers, but custom
      TracingProcessors (SentinelTracer, DashboardEventEmitter) still fire.
      Never call set_tracing_disabled() — that kills ALL processors.

    Args:
        settings: Validated Settings instance from config.py.
    """
    set_default_openai_api("chat_completions")
    if not settings.openai_api_key.strip():
        set_tracing_export_api_key("")
