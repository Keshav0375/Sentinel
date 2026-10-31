"""Exact-match LLM cache — a :class:`Model` that replays a cached response (decision
2026-10-25 C).

``call_with_fallback`` wraps each routed model as ``CachingModel(RetryingModel(model))`` when
the app has a cache (never on the fake LLM), so a hit skips the call and its retries. The
key is everything that shapes the response: the model string, the system instructions, the
input, the model settings, every tool's name and parameter schema, the output schema and
the handoffs. A call is eligible only when it is deterministic — ``temperature == 0`` — or
when ``SENTINEL_CACHE_LLM_DETERMINISTIC`` opts every call in (eval re-runs). Agents set no
temperature, so by default no agent call is cached.

The judge is never cached: it calls LiteLLM directly (``eval/judge``), never through the
router, and a :class:`CachingModel` over any judge model is refused at construction. The
model string is in the key, so a hit only ever replays the model that made it —
``models_used`` stays truthful. A hit reports zero usage: nothing was spent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agents import (
    AgentOutputSchemaBase,
    FunctionTool,
    Handoff,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
    Usage,
)
from agents.items import TResponseOutputItem, TResponseStreamEvent
from pydantic import JsonValue, TypeAdapter, ValidationError

from sentinel.infra.logging import get_logger
from sentinel.memory.cache import LLM_CACHE_TTL_S, Cache, cache_key

__all__ = ["CachingModel"]

logger = get_logger("sentinel.providers.cached_model")

_OUTPUT_ADAPTER: TypeAdapter[list[TResponseOutputItem]] = TypeAdapter(list[TResponseOutputItem])


class CachingModel(Model):
    """*inner* behind an exact-match cache of its deterministic calls."""

    def __init__(self, inner: Model, cache: Cache, *, deterministic: bool) -> None:
        # Imported here: the router imports this module to wrap its models.
        from sentinel.providers.router import JUDGE_MODELS, model_name  # noqa: PLC0415

        self.inner = inner
        self.model = model_name(inner)
        if self.model in JUDGE_MODELS.values():
            raise ValueError(f"the judge model {self.model!r} is never cached")
        self._cache = cache
        self._deterministic = deterministic

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff[Any, Any]],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any | None = None,
    ) -> ModelResponse:
        async def call() -> ModelResponse:
            return await self.inner.get_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt,
            )

        if not (model_settings.temperature == 0 or self._deterministic):
            return await call()
        key = cache_key(
            "llm",
            {
                "model": self.model,
                "system": system_instructions,
                "input": input,
                "settings": model_settings.to_json_dict(),
                "tools": [_tool_key(tool) for tool in tools],
                "output_schema": None
                if output_schema is None or output_schema.is_plain_text()
                else output_schema.json_schema(),
                "handoffs": [handoff.tool_name for handoff in handoffs],
            },
        )
        cached = await self._cache.get(key, "llm")
        replay = _replayed(cached, self.model)
        if replay is not None:
            return replay
        response = await call()
        if response.output:
            dumped: list[JsonValue] = [item.model_dump(mode="json") for item in response.output]
            value: JsonValue = {"output": dumped}
            await self._cache.set(key, "llm", value, LLM_CACHE_TTL_S)
        return response

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff[Any, Any]],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        return self.inner.stream_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )


def _tool_key(tool: Tool) -> dict[str, object]:
    if isinstance(tool, FunctionTool):
        return {"name": tool.name, "params": tool.params_json_schema}
    return {"name": getattr(tool, "name", type(tool).__name__)}


def _replayed(cached: JsonValue | None, model: str) -> ModelResponse | None:
    """The response a cache entry holds; None (a miss) when there is none or it is invalid."""
    if not isinstance(cached, dict):
        return None
    try:
        output = _OUTPUT_ADAPTER.validate_python(cached.get("output"))
    except ValidationError:
        logger.warning("llm_cache_entry_invalid", model=model)
        return None
    return ModelResponse(output=output, usage=Usage(), response_id=None)
