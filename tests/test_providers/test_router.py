"""Tests for sentinel.providers.router — the §4.7 model map, judge rule, and fallback.

No test reaches a provider: Runner.run is replaced with a stub that inspects which model
the cloned agent carries and succeeds or raises accordingly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import litellm
import openai
import pytest
from agents import Agent
from agents.extensions.models.litellm_model import LitellmModel

from sentinel.config import Settings
from sentinel.exceptions import AgentError, ProviderUnavailableError
from sentinel.providers import router
from sentinel.providers.router import (
    JUDGE_MODELS,
    MODEL_MAP,
    ROLES,
    build_model,
    call_with_fallback,
    judge_model_string,
    model_for,
    model_name,
    provider_credential,
    provider_of,
    role_capabilities,
    role_models,
)

SONNET = "anthropic/claude-sonnet-4-6"
HAIKU = "anthropic/claude-haiku-4-5"
GPT4O = "openai/gpt-4o"
GPT4O_MINI = "openai/gpt-4o-mini"

# backend.md §4.7 "Per-Agent Model Assignment", transcribed: role → (Anthropic, OpenAI).
SECTION_4_7 = {
    "orchestrator": (SONNET, GPT4O),
    "triage": (HAIKU, GPT4O_MINI),
    "analysis": (SONNET, GPT4O),
    "reflexion": (HAIKU, GPT4O_MINI),
    "resolution": (SONNET, GPT4O),
    "reverification": (HAIKU, GPT4O_MINI),
    "pr_content_generator": (HAIKU, GPT4O_MINI),
}


def _settings(**kwargs: Any) -> Settings:
    base: dict[str, Any] = {"anthropic_api_key": "sk-ant-test", "openai_api_key": "sk-test"}
    base.update(kwargs)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _agent() -> Agent[Any]:
    return Agent(name="triage_agent", instructions="classify")


@pytest.fixture
def router_log() -> Iterator[MagicMock]:
    with patch.object(router, "logger") as log:
        yield log


def _failing_primary(exc: BaseException, ran: list[str]) -> Any:
    """A Runner.run stand-in: records the model each attempt ran on; anthropic/ raises."""

    async def run(agent: Agent[Any], input: Any, **kwargs: Any) -> MagicMock:
        name = model_name(agent.model)  # type: ignore[arg-type]
        ran.append(name)
        if name.startswith("anthropic/"):
            raise exc
        result = MagicMock(name="RunResult")
        result.kwargs = kwargs
        return result

    return run


def _rate_limit() -> BaseException:
    return litellm.RateLimitError("429", llm_provider="anthropic", model=HAIKU)


def _timeout() -> BaseException:
    return litellm.Timeout("timed out", model=HAIKU, llm_provider="anthropic")


def _api_error() -> BaseException:
    return litellm.APIError(500, "server error", llm_provider="anthropic", model=HAIKU)


def _raw_openai_error() -> BaseException:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APIConnectionError(request=request)


# ── §4.7 model map ────────────────────────────────────────────────────────────


def test_model_map_matches_section_4_7_string_for_string() -> None:
    assert set(MODEL_MAP) == set(SECTION_4_7) == set(ROLES)
    for role, (anthropic_model, openai_model) in SECTION_4_7.items():
        assert dict(MODEL_MAP[role]) == {"anthropic": anthropic_model, "openai": openai_model}


@pytest.mark.parametrize("role", list(SECTION_4_7))
def test_primary_anthropic_runs_anthropic_then_openai(role: str) -> None:
    assert role_models(role, "anthropic") == SECTION_4_7[role]  # type: ignore[arg-type]


@pytest.mark.parametrize("role", list(SECTION_4_7))
def test_primary_openai_swaps_the_pair(role: str) -> None:
    anthropic_model, openai_model = SECTION_4_7[role]
    assert role_models(role, "openai") == (openai_model, anthropic_model)  # type: ignore[arg-type]


@pytest.mark.parametrize("role", list(SECTION_4_7))
def test_model_for_builds_litellm_models_for_the_role(role: str) -> None:
    primary, fallback = model_for(role, _settings())  # type: ignore[arg-type]
    assert isinstance(primary, LitellmModel) and isinstance(fallback, LitellmModel)
    assert (primary.model, fallback.model) == SECTION_4_7[role]
    assert primary.api_key == "sk-ant-test"
    assert fallback.api_key == "sk-test"


def test_model_for_primary_openai() -> None:
    primary, fallback = model_for("analysis", _settings(sentinel_primary_provider="openai"))
    assert model_name(primary) == GPT4O
    assert fallback is not None and model_name(fallback) == SONNET


# ── Judge: always the non-primary provider, never an agent model ─────────────


def test_judge_is_the_non_primary_provider() -> None:
    assert judge_model_string("anthropic") == GPT4O
    assert judge_model_string("openai") == SONNET


@pytest.mark.parametrize("primary", ["anthropic", "openai"])
def test_judge_differs_from_every_agent_model(primary: str) -> None:
    judge = judge_model_string(primary)  # type: ignore[arg-type]
    for role in ROLES:
        assert judge != role_models(role, primary)[0]  # type: ignore[arg-type]
        assert provider_of(role_models(role, primary)[0]) != provider_of(judge)  # type: ignore[arg-type]


def test_judge_equal_to_an_agent_model_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The self-grading guard fires if the judge map ever names an agent model."""
    monkeypatch.setattr(router, "JUDGE_MODELS", {**JUDGE_MODELS, "anthropic": HAIKU})
    with pytest.raises(RuntimeError, match="self-grading"):
        judge_model_string("anthropic")


def test_model_map_check_rejects_a_judge_on_the_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """An import-time check: the judge never runs on the primary provider."""
    monkeypatch.setattr(router, "JUDGE_MODELS", {**JUDGE_MODELS, "openai": "openai/o3"})
    with pytest.raises(RuntimeError):
        router._check_model_map()  # pyright: ignore[reportPrivateUsage]


# ── Credentials: read at use; missing primary → fallback; neither → clear error ──


def test_provider_of_rejects_removed_providers() -> None:
    for model in ("groq/llama-3.1-8b-instant", "azure/gpt-4o", "gemini/x", "gpt-4o", ""):
        with pytest.raises(ValueError, match="anthropic"):
            provider_of(model)


def test_credentials_are_read_at_use_not_at_settings_time() -> None:
    s = _settings(anthropic_api_key=None, openai_api_key=None)
    assert provider_credential("anthropic", s) is None
    s2 = s.model_copy(update={"anthropic_api_key": _settings().anthropic_api_key})
    assert provider_credential("anthropic", s2) == "sk-ant-test"


def test_build_model_names_the_missing_key() -> None:
    with pytest.raises(ProviderUnavailableError, match="ANTHROPIC_API_KEY"):
        build_model(HAIKU, _settings(anthropic_api_key=None))


def test_missing_primary_key_uses_fallback(router_log: MagicMock) -> None:
    primary, fallback = model_for("triage", _settings(anthropic_api_key=None))
    assert model_name(primary) == GPT4O_MINI
    assert fallback is None
    router_log.warning.assert_called_once_with(
        "primary_provider_failed",
        provider="anthropic",
        falling_back_to=GPT4O_MINI,
        role="triage",
        error_type="MissingCredential",
    )


def test_missing_fallback_key_runs_primary_without_fallback() -> None:
    primary, fallback = model_for("triage", _settings(openai_api_key=None))
    assert model_name(primary) == HAIKU
    assert fallback is None


def test_neither_key_raises_a_clear_agent_error() -> None:
    with pytest.raises(ProviderUnavailableError) as info:
        model_for("orchestrator", _settings(anthropic_api_key=None, openai_api_key=None))
    assert isinstance(info.value, AgentError)  # the orchestrator escalates on AgentError
    assert "ANTHROPIC_API_KEY" in str(info.value) and "OPENAI_API_KEY" in str(info.value)


# ── call_with_fallback ────────────────────────────────────────────────────────


async def test_primary_success_does_not_fall_back(router_log: MagicMock) -> None:
    ran: list[str] = []
    with patch("agents.Runner.run", side_effect=_failing_primary(_rate_limit(), ran)):
        routed = await call_with_fallback(
            _agent(), "alert", role="triage", settings=_settings(sentinel_primary_provider="openai")
        )
    assert ran == [GPT4O_MINI]
    assert (routed.model_used, routed.fell_back) == (GPT4O_MINI, False)
    router_log.warning.assert_not_called()


@pytest.mark.parametrize(
    "make_error",
    [_rate_limit, _timeout, _api_error, _raw_openai_error, TimeoutError, asyncio.TimeoutError],
    ids=["RateLimitError", "Timeout", "APIError", "openai.APIError", "TimeoutError", "asyncio"],
)
async def test_primary_error_falls_back_and_logs(make_error: Any, router_log: MagicMock) -> None:
    exc = make_error()
    ran: list[str] = []
    with patch("agents.Runner.run", side_effect=_failing_primary(exc, ran)):
        routed = await call_with_fallback(
            _agent(), "alert", role="triage", settings=_settings(), run_kwargs={"max_turns": 7}
        )

    assert ran == [HAIKU, GPT4O_MINI]
    assert routed.model_used == GPT4O_MINI
    assert routed.fell_back is True
    assert routed.result.kwargs == {"max_turns": 7}  # same run kwargs on the re-run
    router_log.warning.assert_called_once_with(
        "primary_provider_failed",
        provider="anthropic",
        falling_back_to=GPT4O_MINI,
        role="triage",
        error_type=type(exc).__name__,
    )


async def test_other_errors_do_not_fall_back(router_log: MagicMock) -> None:
    ran: list[str] = []
    with (
        patch("agents.Runner.run", side_effect=_failing_primary(ValueError("bad output"), ran)),
        pytest.raises(ValueError, match="bad output"),
    ):
        await call_with_fallback(_agent(), "alert", role="triage", settings=_settings())
    assert ran == [HAIKU]
    router_log.warning.assert_not_called()


async def test_primary_error_without_fallback_propagates() -> None:
    ran: list[str] = []
    with (
        patch("agents.Runner.run", side_effect=_failing_primary(_rate_limit(), ran)),
        pytest.raises(litellm.RateLimitError),
    ):
        await call_with_fallback(
            _agent(), "alert", role="triage", settings=_settings(openai_api_key=None)
        )
    assert ran == [HAIKU]


async def test_missing_primary_key_runs_the_fallback_model() -> None:
    ran: list[str] = []
    with patch("agents.Runner.run", side_effect=_failing_primary(_rate_limit(), ran)):
        routed = await call_with_fallback(
            _agent(), "alert", role="resolution", settings=_settings(anthropic_api_key=None)
        )
    assert ran == [GPT4O]
    assert routed.model_used == GPT4O


async def test_neither_key_raises_before_any_run() -> None:
    with (
        patch("agents.Runner.run") as run,
        pytest.raises(ProviderUnavailableError),
    ):
        await call_with_fallback(
            _agent(),
            "alert",
            role="triage",
            settings=_settings(anthropic_api_key=None, openai_api_key=None),
        )
    run.assert_not_called()


async def test_the_callers_agent_is_not_mutated() -> None:
    agent = _agent()
    with patch("agents.Runner.run", side_effect=_failing_primary(_rate_limit(), [])):
        await call_with_fallback(agent, "alert", role="triage", settings=_settings())
    assert agent.model is None


# ── Capabilities ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("primary", ["anthropic", "openai"])
def test_role_capabilities_are_what_both_providers_support(primary: str) -> None:
    """The agent may run on either side of its pair, so Anthropic's limits always apply."""
    caps = role_capabilities("triage", _settings(sentinel_primary_provider=primary))
    assert caps.supports_structured_outputs is False
    assert caps.strict_schemas is False
