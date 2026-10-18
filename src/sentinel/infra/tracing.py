"""Tracing — LangFuse (observe, prompts, scores) and agent trace capture for eval.

LangFuse (langfuse v3, backend §6) — the surface the agent pipeline uses::

    from sentinel.infra.tracing import load_prompt, observe, push_score, trace_incident

    with trace_incident(incident_id, correlation_id, service) as trace_id:
        # root "incident-pipeline" observation; trace_id == incident_trace_id(incident_id)
        # goes into RunContext and incidents.langfuse_trace_id
        await Runner.run(orchestrator, ..., context=RunContext(..., langfuse_trace_id=trace_id))

    @observe(name="triage-agent", as_type="agent")   # nests under the active trace
    async def run_triage(...): ...

    @observe(name="fetch-logs", as_type="tool")
    async def fetch_logs(...): ...

    prompt = await load_prompt("triage")      # TextPromptClient, or the prompts/ file text
    push_score(trace_id, "triage_accuracy", 0.9)

- ``propagate_attributes`` / ``get_client`` are re-exported from ``langfuse``. ``observe`` is
  langfuse's decorator with the same signature, but ``capture_input`` and ``capture_output``
  default to False: function arguments and return values here are raw logs, alert payloads
  and prompts, and none of that is sent to LangFuse unless a function opts in explicitly
  (``@observe(name=..., capture_output=True)``). Spans still carry name, timing and errors.
  An ``@observe`` root call can instead take ``langfuse_trace_id=incident_trace_id(...)``.
- ``load_prompt`` runs the SDK's synchronous ``get_prompt`` in a worker thread, so a slow or
  unreachable LangFuse never blocks the event loop.
- ``shutdown_traces()`` runs in the FastAPI lifespan on exit; CLI/eval entry points call
  ``flush_traces()`` before returning. Neither raises.
- The client reads ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_BASE_URL``
  from the environment. Without both keys it is disabled: ``@observe`` and ``push_score``
  are no-ops, ``load_prompt`` serves the file, nothing touches the network. A blank key is
  treated as absent, and the SDK's per-call "client will be disabled" warning is muted —
  the lifespan reports the state once (``configure_langfuse()`` → ``langfuse_enabled``).
- LLM generations (R14, decision 2026-10-15): ``LangfuseGenerationBridge`` is an
  openai-agents ``TracingProcessor`` that turns each generation span into a LangFuse
  ``generation`` observation carrying the model, token usage and the agent's registered
  ``TextPromptClient`` (``use_agent_prompts``) — never inputs or outputs. It also sums
  usage into the incident's ``track_incident_tokens`` accumulator, which logs
  ``incident_token_budget_exceeded`` past 50K tokens.
- ``install_trace_processors(...)`` is the one place the openai-agents processors are set:
  exactly Sentinel's own (SentinelTracer, DashboardEventEmitter where wired) plus the
  bridge. It drops the SDK's default exporter, which would upload every span to OpenAI.

Agent trace capture (openai-agents ``TracingProcessor``) — unchanged::

    tracer = SentinelTracer(short_term_memory, trajectories_dir=Path("reports"))
    install_trace_processors(tracer)   # + DashboardEventEmitter in the app

    # In run_pipeline — wrap Runner.run with a named trace so the tracer
    # can associate spans with the correct incident:
    with trace("incident_pipeline", metadata={"incident_id": incident_id}):
        result = await Runner.run(orchestrator, input_text)

The tracer then:
  - Logs each tool call and handoff to ShortTermMemory as a TimelineEntry.
  - On trace completion, writes the full span export to
    ``{trajectories_dir}/{incident_id}.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast, overload
from uuid import UUID

from agents import (
    AgentSpanData,
    FunctionSpanData,
    GenerationSpanData,
    HandoffSpanData,
    Span,
    Trace,
    TracingProcessor,
    set_trace_processors,
)
from langfuse import LangfuseGeneration, get_client, propagate_attributes

# langfuse types observe's transform_to_string as Callable[[Iterable], str] — a bare
# Iterable, which pyright reports as partially unknown.
from langfuse import observe as _langfuse_observe  # pyright: ignore[reportUnknownVariableType]
from langfuse.model import TextPromptClient

from sentinel.infra.logging import get_logger
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.incident import TimelineEntry

__all__ = [
    "INCIDENT_TOKEN_BUDGET",
    "PIPELINE_TRACE_NAME",
    "IncidentTokens",
    "LangfuseGenerationBridge",
    "SentinelTracer",
    "configure_langfuse",
    "current_incident_tokens",
    "flush_traces",
    "get_client",
    "incident_trace_id",
    "langfuse_enabled",
    "load_prompt",
    "observe",
    "propagate_attributes",
    "push_score",
    "generation_bridge",
    "install_trace_processors",
    "shutdown_traces",
    "trace_incident",
    "track_incident_tokens",
    "use_agent_prompts",
]

logger = get_logger("sentinel.infra.tracing")

_SENTINEL_TRACE_KEY = "incident_id"

# ── LangFuse (backend §6) ─────────────────────────────────────────────────────

PIPELINE_TRACE_NAME = "incident-pipeline"
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agents" / "prompts"
_PROMPT_CACHE_TTL_SECONDS = 300
_KEY_VARS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
_ATTR_MAX_CHARS = 200  # propagate_attributes drops (not truncates) longer values


class _MuteKeylessWarning(logging.Filter):
    """Drop the SDK's "initialized without public_key/secret_key" warning.

    A keyless client is a fresh object per ``get_client()`` call, so the SDK would log it on
    every ``@observe`` call; the disabled state is reported once by the lifespan instead.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "Client will be disabled" not in record.getMessage()


def configure_langfuse() -> bool:
    """Treat blank keys as absent and mute the per-call keyless warning; True if enabled.

    The SDK only disables itself when a key is *missing*: a blank one (``KEY=`` in an env
    file) would build a live client with empty credentials that exports to the network.
    Idempotent. Runs at import, and again from the app lifespan — litellm loads ``.env``
    into the environment when it is first imported, which can be after this module.
    """
    for var in _KEY_VARS:
        if var in os.environ and not os.environ[var].strip():
            del os.environ[var]
    sdk_logger = logging.getLogger("langfuse")
    if not any(isinstance(f, _MuteKeylessWarning) for f in sdk_logger.filters):
        sdk_logger.addFilter(_MuteKeylessWarning())
    return langfuse_enabled()


def langfuse_enabled() -> bool:
    """True when both LangFuse keys are set — otherwise the client is a disabled no-op."""
    return all(os.environ.get(var, "").strip() for var in _KEY_VARS)


configure_langfuse()


# The v3 observation types accepted by @observe (backend §6.2).
type ObservationType = Literal[
    "span",
    "generation",
    "embedding",
    "agent",
    "tool",
    "chain",
    "retriever",
    "evaluator",
    "guardrail",
]


@overload
def observe[F: Callable[..., Any]](func: F) -> F: ...


@overload
def observe[F: Callable[..., Any]](
    func: None = None,
    *,
    name: str | None = None,
    as_type: ObservationType | None = None,
    capture_input: bool = False,
    capture_output: bool = False,
    transform_to_string: Callable[[Iterable[Any]], str] | None = None,
) -> Callable[[F], F]: ...


def observe[F: Callable[..., Any]](
    func: F | None = None,
    *,
    name: str | None = None,
    as_type: ObservationType | None = None,
    capture_input: bool = False,
    capture_output: bool = False,
    transform_to_string: Callable[[Iterable[Any]], str] | None = None,
) -> F | Callable[[F], F]:
    """langfuse's ``@observe``, with input/output capture off unless a function opts in.

    Same signature and behaviour as ``langfuse.observe`` (``@observe`` or
    ``@observe(name=..., as_type=...)``, the reserved ``langfuse_trace_id=`` kwarg), except
    that ``capture_input`` / ``capture_output`` default to False instead of on: Sentinel's
    agent and tool arguments are raw logs, alert payloads and prompts, which are not sent
    to LangFuse by default.
    """
    decorator: Callable[[F], F] = _langfuse_observe(
        name=name,
        as_type=as_type,
        capture_input=capture_input,
        capture_output=capture_output,
        transform_to_string=transform_to_string,
    )
    return decorator if func is None else decorator(func)


def incident_trace_id(incident_id: UUID | str) -> str:
    """The LangFuse trace id for an incident: the UUID as 32 lowercase hex (W3C, §4.5)."""
    return UUID(str(incident_id)).hex


@contextmanager
def trace_incident(
    incident_id: UUID | str, correlation_id: UUID | str, service: str
) -> Generator[str]:
    """Open the root ``incident-pipeline`` observation of an incident's trace.

    Equivalent to an ``@observe(name="incident-pipeline")`` root called with
    ``langfuse_trace_id=incident_trace_id(incident_id)``, plus the trace attributes of §6.2
    (``correlation_id`` metadata, ``["incident", service]`` tags) propagated to every child
    observation. Yields the trace id. With LangFuse disabled the observation is a no-op; it still
    yields the same id.
    """
    trace_id = incident_trace_id(incident_id)
    with (
        get_client().start_as_current_observation(
            name=PIPELINE_TRACE_NAME, trace_context={"trace_id": trace_id}
        ),
        propagate_attributes(
            metadata={"correlation_id": str(correlation_id)[:_ATTR_MAX_CHARS]},
            tags=["incident", service[:_ATTR_MAX_CHARS]],
        ),
    ):
        yield trace_id


async def load_prompt(agent_name: str) -> TextPromptClient | str:
    """An agent's system prompt: LangFuse first, ``agents/prompts/{agent_name}.txt`` otherwise.

    Returns the ``TextPromptClient`` (callers use ``.compile(**vars)``; it carries the
    prompt version) or the file text. Any LangFuse failure falls back to the file — a
    disabled client raises in ``get_prompt``. Raises ``FileNotFoundError`` only when both fail.
    """
    try:
        # get_prompt is synchronous and, on a cache miss, does network I/O with retries:
        # run it off the event loop so concurrent incidents keep going meanwhile.
        return await asyncio.to_thread(_fetch_prompt, agent_name)
    except Exception as exc:
        log = logger.warning if langfuse_enabled() else logger.debug
        log("prompt_fallback_to_file", agent_name=agent_name, error=str(exc))
        return (_PROMPTS_DIR / f"{agent_name}.txt").read_text(encoding="utf-8")


def _fetch_prompt(agent_name: str) -> TextPromptClient:
    return get_client().get_prompt(agent_name, cache_ttl_seconds=_PROMPT_CACHE_TTL_SECONDS)


def push_score(trace_id: str, name: str, value: float) -> None:
    """Attach a numeric trace-level score (judge eval dimension, §6.4). No-op when disabled."""
    get_client().create_score(trace_id=trace_id, name=name, value=value, data_type="NUMERIC")


def flush_traces() -> None:
    """Send pending spans and scores now — CLI/eval entry points call this before exiting."""
    try:
        get_client().flush()
    except Exception as exc:
        logger.warning("langfuse_flush_failed", error=str(exc))


def shutdown_traces() -> None:
    """Flush and stop the LangFuse client — the FastAPI lifespan calls this on exit."""
    try:
        get_client().shutdown()
    except Exception as exc:
        logger.warning("langfuse_shutdown_failed", error=str(exc))


# ── LLM generations → LangFuse (R14) and per-incident token totals ────────────

# §6.1 Cost Tracking: alert when a single incident exceeds 50K tokens. The sink is not yet
# named by the architecture, so the alert is a structured warning (decision 2026-10-15).
INCIDENT_TOKEN_BUDGET = 50_000


@dataclass
class IncidentTokens:
    """Token usage summed over every LLM call of one incident (fallback re-runs included)."""

    incident_id: str
    budget: int = INCIDENT_TOKEN_BUDGET
    input_tokens: int = 0
    output_tokens: int = 0
    _warned: bool = field(default=False, repr=False)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, input_tokens: int, output_tokens: int) -> None:
        """Add one call's usage; warns once when the total first exceeds the budget."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if self.total_tokens > self.budget and not self._warned:
            self._warned = True
            logger.warning(
                "incident_token_budget_exceeded",
                incident_id=self.incident_id,
                total_tokens=self.total_tokens,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                budget=self.budget,
            )


_INCIDENT_TOKENS: ContextVar[IncidentTokens | None] = ContextVar(
    "sentinel_incident_tokens", default=None
)
_AGENT_PROMPTS: ContextVar[Mapping[str, TextPromptClient]] = ContextVar(
    "sentinel_agent_prompts", default={}
)


@contextmanager
def track_incident_tokens(
    incident_id: str, *, budget: int = INCIDENT_TOKEN_BUDGET
) -> Generator[IncidentTokens]:
    """Sum the usage of every LLM generation run inside this block (and its tasks)."""
    tokens = IncidentTokens(incident_id=incident_id, budget=budget)
    reset = _INCIDENT_TOKENS.set(tokens)
    try:
        yield tokens
    finally:
        _INCIDENT_TOKENS.reset(reset)


def current_incident_tokens() -> IncidentTokens | None:
    """The accumulator of the enclosing :func:`track_incident_tokens`, if any."""
    return _INCIDENT_TOKENS.get()


@contextmanager
def use_agent_prompts(prompts: Mapping[str, TextPromptClient]) -> Generator[None]:
    """Link generations of the named agents to their LangFuse prompt versions (§6.3).

    Keys are openai-agents agent names; values the ``TextPromptClient`` that agent's
    instructions were compiled from (``load_prompt``). Nested blocks extend the mapping.
    """
    reset = _AGENT_PROMPTS.set({**_AGENT_PROMPTS.get(), **prompts})
    try:
        yield
    finally:
        _AGENT_PROMPTS.reset(reset)


@dataclass
class _OpenGeneration:
    observation: LangfuseGeneration | None
    prompt: TextPromptClient | None


class LangfuseGenerationBridge(TracingProcessor):
    """openai-agents generation spans → LangFuse ``generation`` observations (R14).

    On a generation span's start it opens a LangFuse generation named after the calling
    agent, with the model; on its end it sets ``usage_details`` {input, output} and the
    agent's prompt (if one is registered via :func:`use_agent_prompts`) and ends it. No
    input or output is ever sent: tool results and logs never reach LangFuse. With
    LangFuse disabled it makes no client call at all. Either way the usage is added to the
    current :func:`track_incident_tokens` accumulator.

    Installed beside SentinelTracer and the dashboard emitter by
    :func:`install_trace_processors`. A LangFuse failure is logged and never reaches the
    agent run.
    """

    def __init__(self) -> None:
        self._agent_names: dict[str, str] = {}  # agent span id → agent name
        self._open: dict[str, _OpenGeneration] = {}  # generation span id → observation

    def on_trace_start(self, trace: Trace) -> None:
        pass

    def on_trace_end(self, trace: Trace) -> None:
        pass

    def on_span_start(self, span: Span[Any]) -> None:
        data = span.span_data
        if isinstance(data, AgentSpanData):
            self._agent_names[span.span_id] = data.name
            return
        if not isinstance(data, GenerationSpanData):
            return
        agent_name = self._agent_names.get(span.parent_id or "")
        prompt = _AGENT_PROMPTS.get().get(agent_name) if agent_name else None
        observation: LangfuseGeneration | None = None
        if langfuse_enabled():
            try:
                observation = get_client().start_observation(
                    as_type="generation",
                    name=agent_name or "llm-generation",
                    model=data.model,
                )
            except Exception as exc:
                logger.warning("langfuse_generation_failed", stage="start", error=str(exc))
        self._open[span.span_id] = _OpenGeneration(observation=observation, prompt=prompt)

    def on_span_end(self, span: Span[Any]) -> None:
        data = span.span_data
        if isinstance(data, AgentSpanData):
            self._agent_names.pop(span.span_id, None)
            return
        if not isinstance(data, GenerationSpanData):
            return
        generation = self._open.pop(span.span_id, None)
        input_tokens, output_tokens = _usage_tokens(data.usage)

        tokens = _INCIDENT_TOKENS.get()
        if tokens is not None:
            tokens.add(input_tokens, output_tokens)

        if generation is None or generation.observation is None:
            return
        try:
            update: dict[str, Any] = {
                "usage_details": {"input": input_tokens, "output": output_tokens}
            }
            if generation.prompt is not None:
                update["prompt"] = generation.prompt
            if span.error is not None:
                update["level"] = "ERROR"
            generation.observation.update(**update)
            generation.observation.end()
        except Exception as exc:
            logger.warning("langfuse_generation_failed", stage="end", error=str(exc))

    def shutdown(self) -> None:
        self._agent_names.clear()
        self._open.clear()

    def force_flush(self) -> None:
        pass  # The LangFuse client batches and flushes on its own (flush_traces).


def _usage_tokens(usage: Mapping[str, Any] | None) -> tuple[int, int]:
    """(input, output) tokens from a generation span's usage, 0 where absent."""
    if not usage:
        return 0, 0
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


_bridge: LangfuseGenerationBridge | None = None


def generation_bridge() -> LangfuseGenerationBridge:
    """The process's :class:`LangfuseGenerationBridge` (one, so open generations survive
    a re-install)."""
    global _bridge
    if _bridge is None:
        _bridge = LangfuseGenerationBridge()
    return _bridge


def install_trace_processors(*processors: TracingProcessor) -> list[TracingProcessor]:
    """Set openai-agents' trace processors to exactly *processors* plus the bridge.

    The one place Sentinel wires tracing (decision 2026-10-15): it replaces the SDK's
    default processor, whose exporter uploads every span (prompts, tool results, logs) to
    OpenAI whenever ``OPENAI_API_KEY`` is in the environment. Callers pass all of
    Sentinel's own processors — nothing else is kept.
    """
    installed: list[TracingProcessor] = [*processors, generation_bridge()]
    set_trace_processors(installed)
    return installed


class SentinelTracer(TracingProcessor):
    """Captures every Agents SDK span and writes it to ShortTermMemory + disk.

    Registered globally via ``install_trace_processors(SentinelTracer(...), ...)``.
    Each incident pipeline wraps its ``Runner.run`` call with::

        with trace("incident_pipeline", metadata={"incident_id": inc_id}):
            await Runner.run(orchestrator, ...)

    so spans can be associated with the correct incident via
    ``span.trace_metadata["incident_id"]``.

    Only ``FunctionSpanData`` (tool calls) and ``HandoffSpanData`` (agent
    transfers) produce ``TimelineEntry`` records. ``AgentSpanData`` and
    ``GenerationSpanData`` (LLM calls) are included in the trajectory JSON
    but not surfaced as individual timeline entries to keep the timeline
    focused on observable actions.
    """

    def __init__(
        self,
        short_term_memory: ShortTermMemory,
        *,
        trajectories_dir: Path | None = None,
    ) -> None:
        self._stm = short_term_memory
        self._trajectories_dir = trajectories_dir
        # trace_id → list[dict] collecting every span export for this trace
        self._spans: dict[str, list[dict[str, Any]]] = {}

    # ── TracingProcessor interface ────────────────────────────────────────────

    def on_trace_start(self, trace: Trace) -> None:
        incident_id = _extract_incident_id(trace)
        if incident_id:
            self._spans[trace.trace_id] = []
            logger.debug("trace_start", trace_id=trace.trace_id, incident_id=incident_id)

    def on_trace_end(self, trace: Trace) -> None:
        incident_id = _extract_incident_id(trace)
        spans = self._spans.pop(trace.trace_id, None)
        if incident_id and spans is not None:
            logger.debug(
                "trace_end",
                trace_id=trace.trace_id,
                incident_id=incident_id,
                span_count=len(spans),
            )
            if self._trajectories_dir is not None:
                _save_trajectory(
                    incident_id=incident_id,
                    trace_export=trace.export() or {},
                    spans=spans,
                    trajectories_dir=self._trajectories_dir,
                )

    def on_span_start(self, span: Span[Any]) -> None:
        pass  # We capture data at span end when output is available.

    def on_span_end(self, span: Span[Any]) -> None:
        incident_id = _incident_id_from_span(span)
        if not incident_id:
            return

        # Accumulate span export for trajectory file.
        if span.trace_id in self._spans:
            exported = span.export()
            if exported:
                self._spans[span.trace_id].append(exported)

        # Convert tool-call and handoff spans into timeline entries.
        entry = _span_to_timeline_entry(span)
        if entry is not None:
            try:
                self._stm.add_timeline_entry(incident_id, entry)
            except KeyError:
                pass  # incident was cleared mid-pipeline

    def shutdown(self) -> None:
        self._spans.clear()

    def force_flush(self) -> None:
        pass  # All processing is synchronous; nothing to flush.


# ── Module-level helpers (pure functions, easy to test) ───────────────────────


def _extract_incident_id(trace: Trace) -> str | None:
    """Return the incident_id from trace metadata, or None if not set."""
    metadata = getattr(trace, "metadata", None)
    if isinstance(metadata, dict):
        typed: dict[str, object] = cast(dict[str, object], metadata)
        value = typed.get(_SENTINEL_TRACE_KEY)
        return str(value) if value is not None else None
    return None


def _incident_id_from_span(span: Span[Any]) -> str | None:
    """Return the incident_id from span's inherited trace metadata, or None."""
    metadata = getattr(span, "trace_metadata", None)
    if isinstance(metadata, dict):
        typed: dict[str, object] = cast(dict[str, object], metadata)
        value = typed.get(_SENTINEL_TRACE_KEY)
        return str(value) if value is not None else None
    return None


def _span_to_timeline_entry(span: Span[Any]) -> TimelineEntry | None:
    """Convert a completed span into a TimelineEntry, or return None.

    Only tool-call (FunctionSpanData) and handoff (HandoffSpanData) spans
    produce entries. Agent and generation spans are skipped — they are too
    granular for the incident timeline but are captured in the trajectory file.
    """
    data = span.span_data

    if isinstance(data, FunctionSpanData):
        summary = str(data.output)[:300] if data.output is not None else "no output"
        return TimelineEntry(
            timestamp=_parse_ended_at(span),
            agent_name=_agent_name_from_span(span),
            action=data.name,
            result_summary=summary,
        )

    if isinstance(data, HandoffSpanData):
        from_agent = data.from_agent or "orchestrator"
        to_agent = data.to_agent or "unknown"
        return TimelineEntry(
            timestamp=_parse_ended_at(span),
            agent_name=from_agent,
            action=f"handoff → {to_agent}",
            result_summary=f"Transferred control to {to_agent}",
        )

    return None


def _agent_name_from_span(span: Span[Any]) -> str:
    """Best-effort extraction of the current agent name from a function span."""
    # The trace_metadata can include richer context if set by agent hooks.
    # Fall back to a generic label when no agent context is available.
    metadata = getattr(span, "trace_metadata", None)
    if isinstance(metadata, dict):
        typed: dict[str, object] = cast(dict[str, object], metadata)
        name = typed.get("agent_name")
        if name:
            return str(name)
    return "agent"


def _parse_ended_at(span: Span[Any]) -> datetime:
    """Parse the span's ended_at ISO timestamp, defaulting to now(UTC)."""
    ended_at = getattr(span, "ended_at", None)
    if isinstance(ended_at, str):
        try:
            return datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def _save_trajectory(
    incident_id: str,
    trace_export: dict[str, Any],
    spans: list[dict[str, Any]],
    trajectories_dir: Path,
) -> None:
    """Write a full trajectory JSON file for the eval system.

    Output: ``{trajectories_dir}/{incident_id}.json`` containing:
    - ``trace``: the top-level trace metadata export
    - ``spans``: ordered list of every span export
    - ``saved_at``: ISO timestamp of when the file was written
    """
    trajectories_dir.mkdir(parents=True, exist_ok=True)
    path = trajectories_dir / f"{incident_id}.json"
    payload: dict[str, Any] = {
        "incident_id": incident_id,
        "trace": trace_export,
        "spans": spans,
        "saved_at": datetime.now(UTC).isoformat(),
    }
    try:
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        logger.info("trajectory_saved", incident_id=incident_id, path=str(path))
    except OSError as exc:
        logger.error("trajectory_save_failed", incident_id=incident_id, error=str(exc))
