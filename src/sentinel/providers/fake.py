"""Fake LLM — a deterministic, no-network openai-agents ``Model`` (``SENTINEL_FAKE_LLM=1``).

With ``sentinel_fake_llm`` set, :func:`sentinel.providers.router.model_for` returns a
:class:`FakeModel` for every role instead of a LiteLLM model, so CI and the tests run the
real agent loop (tools, handoffs, output validation, tracing) with no provider and no key.

A fake model answers each call from the active :class:`FakeScript`, keyed by the calling
agent's name and then by its output type's name. Each scripted :class:`FakeTurn` is one
model response: a final output (text, a dict or a Pydantic model, serialised to JSON for a
structured ``output_type``), tool calls the runner then executes, or an error to raise.
Unscripted calls get a deterministic default: a short text, or the smallest JSON value
that satisfies the agent's output schema::

    with fake_script() as script:
        script.on(
            "triage_agent",
            FakeTurn(tool_calls=[FakeToolCall("get_service_metadata", {"service_name": "api"})]),
            FakeTurn(output=TriageResult(...), usage=(1200, 80)),
        )
        script.fail_on("anthropic/claude-haiku-4-5", litellm.RateLimitError(...))
        await Runner.run(agent_with_fake_model, "alert")

Every response reports token usage — scripted, or estimated from the text (4 chars per
token) — on ``ModelResponse.usage`` and on the generation span, exactly where
``LitellmModel`` puts it, so the token bridge and per-incident totals are testable.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, cast

from agents import (
    AgentOutputSchemaBase,
    AgentSpanData,
    Handoff,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
    Usage,
    generation_span,
    get_current_span,
)
from agents.items import TResponseOutputItem, TResponseStreamEvent
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from pydantic import BaseModel

__all__ = [
    "FakeModel",
    "FakeScript",
    "FakeScriptError",
    "FakeToolCall",
    "FakeTurn",
    "active_script",
    "fake_script",
]

_CHARS_PER_TOKEN = 4
_FAKE_ID = "__fake__"


class FakeScriptError(RuntimeError):
    """The fake model cannot produce an output the agent would accept."""


@dataclass(frozen=True)
class FakeToolCall:
    """One tool (or handoff) call the fake model asks the runner to make."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class FakeTurn:
    """One scripted model response.

    Attributes:
        output: The final output — text, or a dict / Pydantic model sent as JSON. Ignored
            when ``tool_calls`` is set (the runner then calls the model again).
        tool_calls: Tool or handoff calls to emit instead of a final output.
        usage: ``(input_tokens, output_tokens)``; estimated from the text when None.
        error: Raised instead of responding (e.g. a litellm RateLimitError).
    """

    output: str | Mapping[str, Any] | BaseModel | None = None
    tool_calls: Sequence[FakeToolCall] = ()
    usage: tuple[int, int] | None = None
    error: BaseException | None = None


class FakeScript:
    """Scripted responses for fake models, consumed in order per agent (or output type)."""

    def __init__(self) -> None:
        self._turns: defaultdict[str, deque[FakeTurn]] = defaultdict(deque)
        self._failures: dict[str, BaseException] = {}
        self.calls: list[tuple[str, str]] = []  # (agent name, model) per model call
        self._call_ids = 0

    def on(self, key: str, *turns: FakeTurn) -> FakeScript:
        """Queue *turns* for the agent named *key* (or the output type named *key*)."""
        self._turns[key].extend(turns)
        return self

    def fail_on(self, model: str, error: BaseException) -> FakeScript:
        """Make every call to the fake model *model* raise *error* (fallback tests)."""
        self._failures[model] = error
        return self

    def next_turn(self, agent_name: str, output_type: str | None, model: str) -> FakeTurn:
        """The turn for this call: the model's failure, else the agent's or type's next."""
        self.calls.append((agent_name, model))
        if model in self._failures:
            return FakeTurn(error=self._failures[model])
        for key in (agent_name, output_type):
            if key is not None and self._turns[key]:
                return self._turns[key].popleft()
        return FakeTurn()

    def next_call_id(self, tool: str) -> str:
        """A tool-call id unique within this script, deterministic across runs."""
        self._call_ids += 1
        return f"fake_call_{self._call_ids}_{tool}"

    def reset(self) -> None:
        self._turns.clear()
        self._failures.clear()
        self.calls.clear()
        self._call_ids = 0


_DEFAULT_SCRIPT = FakeScript()
_ACTIVE: ContextVar[FakeScript | None] = ContextVar("sentinel_fake_script", default=None)


def active_script() -> FakeScript:
    """The script fake models read now: the innermost :func:`fake_script`, or the default."""
    return _ACTIVE.get() or _DEFAULT_SCRIPT


@contextmanager
def fake_script() -> Generator[FakeScript]:
    """A fresh :class:`FakeScript`, active for this context and the tasks it starts."""
    script = FakeScript()
    token = _ACTIVE.set(script)
    try:
        yield script
    finally:
        _ACTIVE.reset(token)


class FakeModel(Model):
    """A deterministic openai-agents ``Model``; never opens a connection.

    ``model`` is the §4.7 string it stands in for, so routing and ``model_used`` read the
    same in fake mode as live; the generation span marks ``model_impl: fake``.
    """

    def __init__(self, model: str, script: FakeScript | None = None) -> None:
        self.model = model
        self._script = script

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
        script = self._script or active_script()
        structured = output_schema is not None and not output_schema.is_plain_text()
        turn = script.next_turn(
            _current_agent_name(),
            output_schema.name() if output_schema is not None else None,
            self.model,
        )
        with generation_span(
            model=self.model,
            model_config={"model_impl": "fake"},
            disabled=tracing.is_disabled(),
        ) as span:
            if turn.error is not None:
                raise turn.error

            items: list[TResponseOutputItem]
            if turn.tool_calls:
                items = [_tool_call_item(script, call) for call in turn.tool_calls]
                out_text = json.dumps([[c.name, dict(c.arguments)] for c in turn.tool_calls])
            else:
                out_text = _output_text(turn.output, output_schema if structured else None)
                items = [_message_item(out_text)]

            in_tokens, out_tokens = turn.usage or (
                _estimate_tokens((system_instructions or "") + _input_text(input)),
                _estimate_tokens(out_text),
            )
            usage = Usage(
                requests=1,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                total_tokens=in_tokens + out_tokens,
            )
            span.span_data.usage = {"input_tokens": in_tokens, "output_tokens": out_tokens}
            return ModelResponse(output=items, usage=usage, response_id=None)

    async def stream_response(
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
        raise NotImplementedError("FakeModel does not stream; Sentinel uses Runner.run")
        yield  # pragma: no cover — makes this an async generator


# ── helpers ───────────────────────────────────────────────────────────────────


def _current_agent_name() -> str:
    span = get_current_span()
    data = span.span_data if span is not None else None
    return data.name if isinstance(data, AgentSpanData) else ""


def _input_text(input: str | list[TResponseInputItem]) -> str:
    return input if isinstance(input, str) else json.dumps(input, default=str, sort_keys=True)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _output_text(
    output: str | Mapping[str, Any] | BaseModel | None,
    schema: AgentOutputSchemaBase | None,
) -> str:
    if isinstance(output, BaseModel):
        return output.model_dump_json()
    if isinstance(output, Mapping):
        return json.dumps(dict(output))
    if isinstance(output, str):
        return output
    if schema is None:
        return "fake response"
    value = _minimal_value(schema.json_schema(), schema.json_schema())
    # Plain (non-object) output types are wrapped in {"response": ...} by the SDK schema.
    return json.dumps(value)


def _minimal_value(node: Mapping[str, Any], root: Mapping[str, Any]) -> Any:
    """The smallest JSON value valid under a JSON-schema *node* (deterministic)."""
    if "$ref" in node:
        ref = cast(str, node["$ref"]).rsplit("/", 1)[-1]
        return _minimal_value(root.get("$defs", {})[ref], root)
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return cast(list[Any], node["enum"])[0]
    if "default" in node:
        return node["default"]
    for key in ("anyOf", "oneOf", "allOf"):
        if key in node:
            options = cast(list[Mapping[str, Any]], node[key])
            non_null = [o for o in options if o.get("type") != "null"]
            return _minimal_value((non_null or options)[0], root)
    kind = node.get("type")
    if isinstance(kind, list):
        kind = next((k for k in cast(list[str], kind) if k != "null"), "null")
    if "pattern" in node or "format" in node:
        raise FakeScriptError(
            f"cannot invent a value for a constrained string {dict(node)!r}; script the "
            "agent's output with FakeScript.on(...)"
        )
    if kind == "object":
        props = cast(Mapping[str, Mapping[str, Any]], node.get("properties", {}))
        required = cast(list[str], node.get("required", list(props)))
        return {name: _minimal_value(props[name], root) for name in required if name in props}
    if kind == "array":
        items = cast(Mapping[str, Any], node.get("items", {}))
        return [_minimal_value(items, root) for _ in range(int(node.get("minItems", 0)))]
    if kind == "string":
        return "x" * max(1, int(node.get("minLength", 1)))
    if kind in ("integer", "number"):
        low = node.get("minimum", node.get("exclusiveMinimum", 0))
        value = low + 1 if "exclusiveMinimum" in node else low
        return int(value) if kind == "integer" else float(value)
    if kind == "boolean":
        return False
    return None


def _message_item(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id=_FAKE_ID,
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def _tool_call_item(script: FakeScript, call: FakeToolCall) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id=_FAKE_ID,
        type="function_call",
        call_id=script.next_call_id(call.name),
        name=call.name,
        arguments=json.dumps(dict(call.arguments)),
        status="completed",
    )
