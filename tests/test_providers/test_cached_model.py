"""Tests for sentinel.providers.cached_model — the exact-match LLM cache — and its wiring
in ``call_with_fallback``. The judge never goes through it.

No network: the inner model counts its calls, the cache is an in-memory stand-in.
"""

from __future__ import annotations

from typing import Any

import pytest
from agents import (
    Agent,
    FunctionTool,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Usage,
    function_tool,
)
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import JsonValue

from sentinel.config import Settings
from sentinel.eval.judge import FakeJudgeClient, evaluate_trajectory
from sentinel.memory.cache import LLM_CACHE_TTL_S, CacheKind
from sentinel.providers import router
from sentinel.providers.cached_model import CachingModel
from sentinel.providers.fake import FakeModel, fake_script
from sentinel.providers.router import JUDGE_MODELS, call_with_fallback


class _DictCache:
    """The Cache protocol over a dict; records gets and sets."""

    def __init__(self) -> None:
        self.data: dict[str, JsonValue] = {}
        self.gets: list[str] = []
        self.sets: list[tuple[str, int]] = []

    async def get(self, key: str, kind: CacheKind) -> JsonValue | None:
        self.gets.append(key)
        return self.data.get(key)

    async def set(self, key: str, kind: CacheKind, value: JsonValue, ttl_s: int) -> None:
        self.sets.append((key, ttl_s))
        self.data[key] = value


class _Counting(Model):
    """A model that answers with its own name and a call number; counts its calls."""

    def __init__(self, model: str = "anthropic/claude-haiku-4-5") -> None:
        self.model = model
        self.calls = 0

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.calls += 1
        message = ResponseOutputMessage(
            id="msg_1",
            type="message",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(
                    type="output_text", text=f"{self.model} #{self.calls}", annotations=[]
                )
            ],
        )
        return ModelResponse(
            output=[message],
            usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15),
            response_id=None,
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@function_tool
def _lookup(service: str) -> str:
    """Look a service up."""
    return service


async def _ask(
    model: Model,
    *,
    text: str = "the alert",
    settings: ModelSettings | None = None,
    tools: list[Any] | None = None,
) -> ModelResponse:
    return await model.get_response(
        "system prompt",
        text,
        settings or ModelSettings(),
        tools or [],
        None,
        [],
        ModelTracing.DISABLED,
    )


def _text(response: ModelResponse) -> str:
    item = response.output[0]
    assert isinstance(item, ResponseOutputMessage)
    part = item.content[0]
    assert isinstance(part, ResponseOutputText)
    return part.text


async def test_not_cached_without_temperature_zero_or_flag() -> None:
    inner, cache = _Counting(), _DictCache()
    model = CachingModel(inner, cache, deterministic=False)

    await _ask(model)
    await _ask(model, settings=ModelSettings(temperature=0.7))

    assert inner.calls == 2
    assert cache.gets == [] and cache.sets == []


async def test_cached_when_deterministic_flag() -> None:
    inner, cache = _Counting(), _DictCache()
    model = CachingModel(inner, cache, deterministic=True)

    first = await _ask(model, tools=[_lookup])
    second = await _ask(model, tools=[_lookup])

    assert inner.calls == 1
    assert _text(second) == _text(first) == "anthropic/claude-haiku-4-5 #1"
    assert second.usage.total_tokens == 0 and second.usage.requests == 0  # nothing spent
    assert first.usage.total_tokens == 15
    assert [ttl for _, ttl in cache.sets] == [LLM_CACHE_TTL_S] == [86400]
    # A different input, settings or tool set is a different key.
    await _ask(model, text="another alert", tools=[_lookup])
    await _ask(model, tools=[])
    assert inner.calls == 3


async def test_cached_when_temperature_zero() -> None:
    inner, cache = _Counting(), _DictCache()
    model = CachingModel(inner, cache, deterministic=False)
    zero = ModelSettings(temperature=0)

    await _ask(model, settings=zero)
    await _ask(model, settings=zero)

    assert inner.calls == 1


async def test_key_includes_model() -> None:
    cache = _DictCache()
    haiku, mini = _Counting("anthropic/claude-haiku-4-5"), _Counting("openai/gpt-4o-mini")
    first = CachingModel(haiku, cache, deterministic=True)
    second = CachingModel(mini, cache, deterministic=True)

    await _ask(first)
    replay = await _ask(second)

    assert (haiku.calls, mini.calls) == (1, 1)  # no shared hit across models
    assert _text(replay).startswith("openai/gpt-4o-mini")  # models_used stays truthful
    assert second.model == "openai/gpt-4o-mini"


@pytest.mark.parametrize("judge", sorted(set(JUDGE_MODELS.values())))
def test_judge_model_never_cached(judge: str) -> None:
    with pytest.raises(ValueError, match="never cached"):
        CachingModel(_Counting(judge), _DictCache(), deterministic=True)


async def test_judge_calls_bypass_cache() -> None:
    """The judge calls its client directly: the LLM cache has no seam there, so even with
    every agent call opted in, both judge runs reach the model."""
    settings = Settings(_env_file=None, sentinel_cache_llm_deterministic=True)  # type: ignore[call-arg]
    assert settings.sentinel_cache_llm_deterministic
    client = FakeJudgeClient()
    for _ in range(2):
        await evaluate_trajectory(
            {"timeline": [], "pipeline_result": {"models_used": []}},
            {"branch": "b", "case": "c", "expected_resolution": "rollback"},
            incident_id="inc-1",
            scenario_id="s-1",
            client=client,
            model=JUDGE_MODELS["anthropic"],
        )
    assert len(client.calls) == 2


async def test_fake_llm_not_wrapped() -> None:
    cache = _DictCache()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, sentinel_fake_llm=True, sentinel_cache_llm_deterministic=True
    )
    agent = Agent(name="triage", instructions="triage it")
    with fake_script() as script:
        await call_with_fallback(agent, "the alert", role="triage", settings=settings, cache=cache)
        await call_with_fallback(agent, "the alert", role="triage", settings=settings, cache=cache)
    assert cache.gets == [] and cache.sets == []
    assert len(script.calls) == 2


async def test_call_with_fallback_wraps_with_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a cache (and not the fake LLM) a repeated deterministic run is served from it."""
    settings = Settings(_env_file=None, sentinel_cache_llm_deterministic=True)  # type: ignore[call-arg]
    inner = FakeModel("anthropic/claude-haiku-4-5")
    monkeypatch.setattr(router, "model_for", lambda role, settings: (inner, None))
    cache = _DictCache()
    agent = Agent(name="triage", instructions="triage it")
    with fake_script() as script:
        first = await call_with_fallback(
            agent, "the alert", role="triage", settings=settings, cache=cache
        )
        second = await call_with_fallback(
            agent, "the alert", role="triage", settings=settings, cache=cache
        )
        uncached = await call_with_fallback(agent, "the alert", role="triage", settings=settings)
    assert len(script.calls) == 2  # the first and the uncached run; the second hit
    assert second.result.final_output == first.result.final_output
    assert second.model_used == first.model_used == uncached.model_used


async def test_invalid_cached_entry_is_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    inner, cache = _Counting(), _DictCache()
    model = CachingModel(inner, cache, deterministic=True)
    await _ask(model)
    (key,) = cache.data
    cache.data[key] = {"output": [{"type": "not-an-item"}]}

    response = await _ask(model)

    assert inner.calls == 2
    assert _text(response) == "anthropic/claude-haiku-4-5 #2"
    assert cache.data[key] != {"output": [{"type": "not-an-item"}]}  # replaced by the call


def test_function_tool_helper_is_a_function_tool() -> None:
    assert isinstance(_lookup, FunctionTool)
