"""Agent trace capture — records every tool call and handoff for eval.

Usage::

    tracer = SentinelTracer(short_term_memory, trajectories_dir=Path("reports"))
    add_trace_processor(tracer)

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

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from agents import (
    FunctionSpanData,
    HandoffSpanData,
    Span,
    Trace,
    TracingProcessor,
)

from sentinel.infra.logging import get_logger
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.incident import TimelineEntry

logger = get_logger("sentinel.infra.tracing")

_SENTINEL_TRACE_KEY = "incident_id"


class SentinelTracer(TracingProcessor):
    """Captures every Agents SDK span and writes it to ShortTermMemory + disk.

    Registered globally via ``add_trace_processor(SentinelTracer(...))``.
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
