"""Provider layer — §4.7 model routing with fallback, capability flags, SDK defaults.

Public API:
    model_for(role, settings) → (primary Model, fallback Model | None)
    call_with_fallback(agent, input, *, role, settings, run_kwargs) → RoutedRun
    role_capabilities(role, settings) → ProviderCapabilities
    judge_model_string(primary) → str
    apply_sdk_defaults(settings) → None
    coerce_output(text, schema) → BaseModel | None
"""

from __future__ import annotations

from agents import set_default_openai_api, set_tracing_export_api_key

from sentinel.config import Settings
from sentinel.providers.capabilities import ProviderCapabilities, get_capabilities
from sentinel.providers.output_coercion import coerce_output
from sentinel.providers.router import (
    Role,
    RoutedRun,
    call_with_fallback,
    judge_model_string,
    model_for,
    role_capabilities,
)

__all__ = [
    "ProviderCapabilities",
    "Role",
    "RoutedRun",
    "apply_sdk_defaults",
    "call_with_fallback",
    "coerce_output",
    "get_capabilities",
    "judge_model_string",
    "model_for",
    "role_capabilities",
]


def apply_sdk_defaults(settings: Settings) -> None:
    """Configure the OpenAI Agents SDK for the active provider.

    Must be called once at application startup, before any agents are built.
    Centralises the two SDK-global calls that were previously scattered across
    main.py, run_scenario.py, and eval/runner.py.

    - Forces chat-completions API mode (the default 'responses' API is OpenAI-only;
      agents run through LiteLLM on Anthropic too).
    - Sets a blank tracing export key when no OpenAI key is configured so the
      SDK won't try to export traces to OpenAI's servers, but custom
      TracingProcessors (SentinelTracer, DashboardEventEmitter) still fire.
      Never call set_tracing_disabled() — that kills ALL processors.

    Args:
        settings: Validated Settings instance from config.py.
    """
    set_default_openai_api("chat_completions")
    if settings.openai_api_key is None:
        set_tracing_export_api_key("")
