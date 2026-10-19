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

The orchestrator's two loop checks (backend §4.3, §4.4) have turn builders, keyed by their
agent names (decision 2026-10-15)::

    script.on(
        REFLEXION_AGENT,
        reflexion_turn(0.4, next_tool_call=FetchLogsCall(...)),   # loop back to Analysis
        reflexion_turn(0.85, deploy=SuspectDeploy(commit_sha="a1b2c3d", pr_number=42)),
    )
    script.on(REVERIFICATION_AGENT, reverification_turn("FAIL", "wrong target"),
              reverification_turn("PASS"))

Real models frame their JSON — a sentence before it, a code fence, a note after it — and
get the bookkeeping fields slightly wrong. A *framing* wraps every JSON output of agents
without structured outputs, script-wide or per turn, and ``inconsistent=True`` makes a
reflexion reply contradict itself the way models do (the contract repairs it)::

    script.frame_outputs("prose_fence")          # "Here is my analysis:\n```json\n{..}\n```"
    script.on(REFLEXION_AGENT, reflexion_turn(0.85, deploy=..., inconsistent=True))

Unscripted, they answer with the cautious outcome: reflexion with confidence 0.0 and a
``fetch_logs`` next call, reverification with an ``ESCALATE``.

The revert-PR writer (§3.4) has one too, keyed by ``PR_CONTENT_AGENT``: a compliant reply
for a spec, or a deliberately non-compliant one that drives the deterministic fallbacks::

    script.on(PR_CONTENT_AGENT, pr_content_turn(spec), pr_content_turn(spec, compliant=False))
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args

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

from sentinel.models.reflexion import (
    DeployDetailsCall,
    FetchLogsCall,
    ReflexionResult,
    SuspectDeploy,
)
from sentinel.models.reverification import ReverificationOutcome
from sentinel.models.tools import LogFetchInput, RollbackSpec

__all__ = [
    "FRAMINGS",
    "PR_CONTENT_AGENT",
    "REFLEXION_AGENT",
    "REVERIFICATION_AGENT",
    "FakeModel",
    "FakeScript",
    "FakeScriptError",
    "FakeToolCall",
    "FakeTurn",
    "Framing",
    "active_script",
    "fake_script",
    "frame",
    "pr_content_turn",
    "reflexion_turn",
    "reverification_turn",
]

_CHARS_PER_TOKEN = 4
_FAKE_ID = "__fake__"

# Agent names of the orchestrator's loop checks (decision 2026-10-15).
REFLEXION_AGENT = "reflexion"
REVERIFICATION_AGENT = "reverification"
# Agent name of the revert-PR writer (backend §3.4).
PR_CONTENT_AGENT = "pr_content_generator"


# How a model wraps its JSON reply: bare (as-is), a fenced block, prose then a fence, an
# inline fence then a trailing note, prose then the bare object.
Framing = Literal["bare", "fence", "prose_fence", "fence_note", "prose_bare"]
FRAMINGS: tuple[Framing, ...] = get_args(Framing)


def frame(text: str, framing: Framing) -> str:
    """*text* (a JSON reply) wrapped the way a chat model often sends it."""
    if framing == "fence":
        return f"```json\n{text}\n```"
    if framing == "prose_fence":
        return f"Here is my answer:\n```json\n{text}\n```"
    if framing == "fence_note":
        return f"```json{text}```\n\nNote: this is based on the {{evidence}} gathered so far."
    if framing == "prose_bare":
        return f"Result:\n{text}\nLet me know if you need more."
    return text


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
        framing: How a JSON output is wrapped as text (agents without structured
            outputs only); None uses the script's :meth:`FakeScript.frame_outputs`.
    """

    output: str | Mapping[str, Any] | BaseModel | None = None
    tool_calls: Sequence[FakeToolCall] = ()
    usage: tuple[int, int] | None = None
    error: BaseException | None = None
    framing: Framing | None = None


class FakeScript:
    """Scripted responses for fake models, consumed in order per agent (or output type)."""

    def __init__(self) -> None:
        self._turns: defaultdict[str, deque[FakeTurn]] = defaultdict(deque)
        self._failures: dict[str, BaseException] = {}
        self.calls: list[tuple[str, str]] = []  # (agent name, model) per model call
        self.framing: Framing = "bare"
        self._call_ids = 0

    def on(self, key: str, *turns: FakeTurn) -> FakeScript:
        """Queue *turns* for the agent named *key* (or the output type named *key*)."""
        self._turns[key].extend(turns)
        return self

    def frame_outputs(self, framing: Framing) -> FakeScript:
        """Wrap every JSON output (of agents without structured outputs) in *framing*."""
        self.framing = framing
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
        return _unscripted_turn(agent_name, output_type)

    def next_call_id(self, tool: str) -> str:
        """A tool-call id unique within this script, deterministic across runs."""
        self._call_ids += 1
        return f"fake_call_{self._call_ids}_{tool}"

    def reset(self) -> None:
        self._turns.clear()
        self._failures.clear()
        self.calls.clear()
        self.framing = "bare"
        self._call_ids = 0


def reflexion_turn(
    confidence: float,
    *,
    deploy: SuspectDeploy | None = None,
    next_tool_call: FetchLogsCall | DeployDetailsCall | None = None,
    supporting_evidence: Sequence[str] = (),
    missing_or_contradictory: Sequence[str] = (),
    additional_data_needed: Sequence[str] = (),
    inconsistent: bool = False,
    usage: tuple[int, int] | None = None,
) -> FakeTurn:
    """A ``reflexion`` reply (§4.3), validated as a :class:`ReflexionResult` now.

    ``specific_deploy_identified`` follows ``deploy``; the result is repaired exactly as a
    model's reply would be (confidence clamped, a call above 0.7 dropped).

    ``inconsistent=True`` sends the JSON a careless model would, which repairs to the same
    result: ``specific_deploy_identified`` contradicts ``deploy``, the SHA is padded with
    spaces, an empty list is null, and a confident reply still names a next tool call.
    """
    result = ReflexionResult(
        confidence=confidence,
        supporting_evidence=list(supporting_evidence),
        missing_or_contradictory=list(missing_or_contradictory),
        additional_data_needed=list(additional_data_needed),
        specific_deploy_identified=deploy is not None,
        deploy=deploy,
        next_tool_call=next_tool_call,
    )
    if not inconsistent:
        return FakeTurn(output=result, usage=usage)
    reply: dict[str, Any] = result.model_dump(mode="json")
    reply["specific_deploy_identified"] = deploy is None
    if deploy is not None:
        reply["deploy"]["commit_sha"] = f" {deploy.commit_sha} "
    for name in ("supporting_evidence", "missing_or_contradictory", "additional_data_needed"):
        if not reply[name]:
            reply[name] = None
    if reply["next_tool_call"] is None:
        stray = FetchLogsCall(tool="fetch_logs", args=LogFetchInput(service="unknown"))
        reply["next_tool_call"] = stray.model_dump(mode="json")
    return FakeTurn(output=reply, usage=usage)


def reverification_turn(
    outcome: ReverificationOutcome | str,
    reason: str | None = None,
    *,
    usage: tuple[int, int] | None = None,
) -> FakeTurn:
    """A ``reverification`` reply (§4.4): ``PASS``, or ``<outcome>: <reason>``.

    The text is sent as-is, unvalidated, so a test can script a malformed reply
    (``reverification_turn("looks fine")``) and check that it parses to ESCALATE.
    """
    text = str(outcome) if reason is None else f"{outcome}: {reason}"
    return FakeTurn(output=text, usage=usage)


def pr_content_turn(
    spec: RollbackSpec,
    *,
    compliant: bool = True,
    title: str | None = None,
    description: str | None = None,
    usage: tuple[int, int] | None = None,
) -> FakeTurn:
    """A ``pr_content_generator`` reply (§3.4) for *spec*: the title, a blank line, the body.

    Compliant: a ``revert:`` title under 72 characters with the PR # and incident id (for
    PR numbers up to 6 digits and an id up to 36 characters), and a 100-200 word body with
    the four sections and the confidence. ``compliant=False`` breaks every rule (no prefix,
    PR # or id; no sections; no confidence). *title* / *description* replace either part.
    """
    pr, sha = spec.target_deploy.pr_number, spec.target_deploy.commit_sha
    if compliant:
        default_title = f"revert: undo PR #{pr} (incident {spec.incident_id})"
        default_description = (
            "## Incident\n\n"
            f"Sentinel incident {spec.incident_id} found the service failing its health "
            f"checks right after PR #{pr} shipped commit {sha}. The failures started with "
            "that deploy and have not stopped since.\n\n"
            "## Root Cause\n\n"
            f"PR #{pr} (commit {sha}) changed how the service reports its health, so the "
            "checks now fail whenever a dependency is degraded.\n\n"
            "## Evidence\n\n"
            f"- The first failed check came minutes after commit {sha} was deployed.\n"
            "- No other deploy landed in the incident window.\n"
            "- The previous version passed the same checks.\n\n"
            "## Rollback\n\n"
            f"Merging this pull request reverts commit {sha}, and the deployment pipeline "
            "then redeploys the previous version. Closing it leaves the current deploy in "
            "place and hands the incident to an engineer to handle manually. "
            f"Confidence: {spec.confidence:.2f}. You decide whether to merge."
        )
    else:
        default_title = "Rollback PR"
        default_description = "The last deploy looks broken. Please revert it soon."
    text = f"{title or default_title}\n\n{description or default_description}"
    return FakeTurn(output=text, usage=usage)


def _unscripted_reflexion() -> ReflexionResult:
    return ReflexionResult(
        confidence=0.0,
        supporting_evidence=[],
        missing_or_contradictory=["unscripted fake reflexion: no evidence assessed"],
        additional_data_needed=[],
        specific_deploy_identified=False,
        deploy=None,
        next_tool_call=FetchLogsCall(tool="fetch_logs", args=LogFetchInput(service="unknown")),
    )


# Unscripted replies the generic defaults cannot give: structured outputs whose model
# validator the schema-minimal value breaks (by output type name), and plain-text tokens
# (by agent name, for agents with no structured output).
_UNSCRIPTED_STRUCTURED: dict[str, Callable[[], BaseModel]] = {
    ReflexionResult.__name__: _unscripted_reflexion,
}
_UNSCRIPTED_TEXT: dict[str, str] = {
    REVERIFICATION_AGENT: "ESCALATE: unscripted fake reverification",
}


def _unscripted_turn(agent_name: str, output_type: str | None) -> FakeTurn:
    if output_type in _UNSCRIPTED_STRUCTURED:
        return FakeTurn(output=_UNSCRIPTED_STRUCTURED[output_type]())
    if output_type is None and agent_name == REFLEXION_AGENT:
        # Without structured outputs the reflexion agent still answers its JSON as text.
        return FakeTurn(output=_unscripted_reflexion())
    if output_type in (None, "str") and agent_name in _UNSCRIPTED_TEXT:
        return FakeTurn(output=_UNSCRIPTED_TEXT[agent_name])
    return FakeTurn()


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
                if not structured and isinstance(turn.output, BaseModel | Mapping):
                    out_text = frame(out_text, turn.framing or script.framing)
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
