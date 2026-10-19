"""Tests for sentinel.infra.tracing — LangFuse helpers, SentinelTracer and its helpers.

All offline: the suite runs without LangFuse keys (tests/conftest.py removes them), so the
real client is the disabled no-op; tests that need a live client mock ``get_client``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from langfuse.api.resources.prompts import Prompt_Text
from langfuse.model import TextPromptClient

from sentinel import main
from sentinel.agents import loader
from sentinel.agents.loader import UNTRUSTED_DATA_PREAMBLE
from sentinel.infra import tracing
from sentinel.infra.tracing import (
    PIPELINE_TRACE_NAME,
    SentinelTracer,
    _extract_incident_id,
    _incident_id_from_span,
    _parse_ended_at,
    _save_trajectory,
    _span_to_timeline_entry,
    configure_langfuse,
    flush_traces,
    incident_trace_id,
    langfuse_enabled,
    load_prompt,
    observe,
    push_score,
    shutdown_traces,
    trace_incident,
)
from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import IncidentAlert

# ── Mock helpers ──────────────────────────────────────────────────────────────


def _mock_trace(incident_id: str | None = "inc-test0001", name: str = "test") -> Any:
    """Create a minimal Trace mock with metadata."""
    t = MagicMock()
    t.trace_id = "trace_abc123"
    t.name = name
    t.metadata = {"incident_id": incident_id} if incident_id is not None else {}
    t.export.return_value = {"workflow_name": name, "id": "trace_abc123"}
    return t


def _mock_span(
    trace_id: str = "trace_abc123",
    incident_id: str | None = "inc-test0001",
    span_data: Any = None,
    ended_at: str | None = None,
) -> Any:
    """Create a minimal Span mock with trace_metadata."""
    s = MagicMock()
    s.trace_id = trace_id
    s.span_id = "span_xyz456"
    s.trace_metadata = {"incident_id": incident_id} if incident_id else {}
    s.span_data = span_data or MagicMock()
    s.ended_at = ended_at or datetime.now(UTC).isoformat()
    s.export.return_value = {"id": "span_xyz456", "type": "test"}
    return s


def _function_span_data(name: str = "fetch_logs", output: str = "10 log entries") -> Any:
    """Create a FunctionSpanData mock."""
    from agents import FunctionSpanData

    return FunctionSpanData(name=name, input="query", output=output)


def _handoff_span_data(from_agent: str = "orchestrator", to_agent: str = "triage_agent") -> Any:
    """Create a HandoffSpanData mock."""
    from agents import HandoffSpanData

    return HandoffSpanData(from_agent=from_agent, to_agent=to_agent)


def _make_stm(incident_id: str = "inc-test0001") -> ShortTermMemory:
    stm = ShortTermMemory()
    alert = IncidentAlert(title="error_rate", severity="critical", service="api-gateway")
    stm.create(incident_id, alert)
    return stm


# ── _extract_incident_id ──────────────────────────────────────────────────────


def test_extract_incident_id_found() -> None:
    trace = _mock_trace("inc-abc12345")
    assert _extract_incident_id(trace) == "inc-abc12345"


def test_extract_incident_id_missing_key() -> None:
    trace = _mock_trace()
    trace.metadata = {"other": "value"}
    assert _extract_incident_id(trace) is None


def test_extract_incident_id_no_metadata() -> None:
    trace = _mock_trace()
    trace.metadata = None
    assert _extract_incident_id(trace) is None


def test_extract_incident_id_not_dict() -> None:
    trace = _mock_trace()
    trace.metadata = "not-a-dict"
    assert _extract_incident_id(trace) is None


def test_extract_incident_id_converts_to_str() -> None:
    trace = _mock_trace()
    trace.metadata = {"incident_id": 42}  # non-string value
    result = _extract_incident_id(trace)
    assert result == "42"


# ── _incident_id_from_span ────────────────────────────────────────────────────


def test_incident_id_from_span_found() -> None:
    span = _mock_span(incident_id="inc-span0001")
    assert _incident_id_from_span(span) == "inc-span0001"


def test_incident_id_from_span_none_metadata() -> None:
    span = _mock_span()
    span.trace_metadata = None
    assert _incident_id_from_span(span) is None


def test_incident_id_from_span_empty_metadata() -> None:
    span = _mock_span(incident_id=None)
    assert _incident_id_from_span(span) is None


# ── _span_to_timeline_entry ───────────────────────────────────────────────────


def test_span_to_entry_function_span() -> None:
    data = _function_span_data("fetch_logs", "15 entries found")
    span = _mock_span(span_data=data)
    entry = _span_to_timeline_entry(span)
    assert entry is not None
    assert entry.action == "fetch_logs"
    assert "15 entries found" in entry.result_summary


def test_span_to_entry_function_span_no_output() -> None:
    data = _function_span_data("fetch_logs", None)
    data.output = None
    span = _mock_span(span_data=data)
    entry = _span_to_timeline_entry(span)
    assert entry is not None
    assert entry.result_summary == "no output"


def test_span_to_entry_function_span_truncates_long_output() -> None:
    long_output = "x" * 400
    data = _function_span_data("big_tool", long_output)
    span = _mock_span(span_data=data)
    entry = _span_to_timeline_entry(span)
    assert entry is not None
    assert len(entry.result_summary) <= 300


def test_span_to_entry_handoff_span() -> None:
    data = _handoff_span_data("orchestrator", "triage_agent")
    span = _mock_span(span_data=data)
    entry = _span_to_timeline_entry(span)
    assert entry is not None
    assert entry.agent_name == "orchestrator"
    assert "triage_agent" in entry.action
    assert "triage_agent" in entry.result_summary


def test_span_to_entry_handoff_none_from_agent() -> None:
    from agents import HandoffSpanData

    data = HandoffSpanData(from_agent=None, to_agent="comms_agent")
    span = _mock_span(span_data=data)
    entry = _span_to_timeline_entry(span)
    assert entry is not None
    assert entry.agent_name == "orchestrator"  # defaults to "orchestrator"


def test_span_to_entry_other_span_type_returns_none() -> None:
    from agents import AgentSpanData, GenerationSpanData

    for data in [AgentSpanData("triage_agent"), GenerationSpanData()]:
        span = _mock_span(span_data=data)
        assert _span_to_timeline_entry(span) is None


# ── _parse_ended_at ───────────────────────────────────────────────────────────


def test_parse_ended_at_valid_iso() -> None:
    span = _mock_span(ended_at="2026-05-12T15:30:00Z")
    result = _parse_ended_at(span)
    assert result.year == 2026
    assert result.month == 5


def test_parse_ended_at_none_defaults_to_now() -> None:
    span = _mock_span()
    span.ended_at = None
    before = datetime.now(UTC)
    result = _parse_ended_at(span)
    assert result >= before


def test_parse_ended_at_invalid_string_defaults_to_now() -> None:
    span = _mock_span()
    span.ended_at = "not-a-date"
    before = datetime.now(UTC)
    result = _parse_ended_at(span)
    assert result >= before


# ── _save_trajectory ──────────────────────────────────────────────────────────


def test_save_trajectory_creates_file(tmp_path: Path) -> None:
    _save_trajectory(
        incident_id="inc-savetest",
        trace_export={"workflow_name": "test"},
        spans=[{"type": "function", "name": "fetch_logs"}],
        trajectories_dir=tmp_path,
    )
    output = tmp_path / "inc-savetest.json"
    assert output.exists()


def test_save_trajectory_valid_json(tmp_path: Path) -> None:
    _save_trajectory(
        incident_id="inc-jsontest",
        trace_export={"id": "trace_123"},
        spans=[{"type": "handoff"}],
        trajectories_dir=tmp_path,
    )
    data = json.loads((tmp_path / "inc-jsontest.json").read_text())
    assert data["incident_id"] == "inc-jsontest"
    assert data["trace"]["id"] == "trace_123"
    assert len(data["spans"]) == 1
    assert "saved_at" in data


def test_save_trajectory_creates_parent_dirs(tmp_path: Path) -> None:
    deep_dir = tmp_path / "a" / "b" / "c"
    _save_trajectory(
        incident_id="inc-deeptest",
        trace_export={},
        spans=[],
        trajectories_dir=deep_dir,
    )
    assert (deep_dir / "inc-deeptest.json").exists()


# ── SentinelTracer ────────────────────────────────────────────────────────────


def test_tracer_on_span_end_tool_call_adds_timeline_entry() -> None:
    stm = _make_stm()
    tracer = SentinelTracer(stm)
    # Prime the internal spans dict so on_span_end knows about this trace
    tracer._spans["trace_abc123"] = []

    data = _function_span_data("get_service_metadata", "api-gateway metadata")
    span = _mock_span(span_data=data)
    tracer.on_span_end(span)

    timeline = stm.get_timeline("inc-test0001")
    assert len(timeline) == 1
    assert timeline[0].action == "get_service_metadata"


def test_tracer_on_span_end_handoff_adds_timeline_entry() -> None:
    stm = _make_stm()
    tracer = SentinelTracer(stm)
    tracer._spans["trace_abc123"] = []

    data = _handoff_span_data("orchestrator", "log_analyst_agent")
    span = _mock_span(span_data=data)
    tracer.on_span_end(span)

    timeline = stm.get_timeline("inc-test0001")
    assert len(timeline) == 1
    assert "log_analyst_agent" in timeline[0].action


def test_tracer_on_span_end_no_incident_id_skips() -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm)

    span = _mock_span(incident_id=None)
    tracer.on_span_end(span)  # must not raise

    assert stm.active_count == 0


def test_tracer_on_span_end_cleared_incident_does_not_raise() -> None:
    """If the incident is cleared between span start and end, no KeyError."""
    stm = _make_stm()
    tracer = SentinelTracer(stm)
    tracer._spans["trace_abc123"] = []
    stm.clear("inc-test0001")  # clear before span ends

    data = _function_span_data()
    span = _mock_span(span_data=data)
    tracer.on_span_end(span)  # must not raise


def test_tracer_on_trace_start_primes_spans_dict() -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm)
    trace = _mock_trace("inc-primetest")
    tracer.on_trace_start(trace)
    assert "trace_abc123" in tracer._spans


def test_tracer_on_trace_start_ignores_non_sentinel_traces() -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm)
    trace = _mock_trace(incident_id=None)
    trace.metadata = {}  # no incident_id
    tracer.on_trace_start(trace)
    assert "trace_abc123" not in tracer._spans


def test_tracer_on_trace_end_writes_trajectory(tmp_path: Path) -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm, trajectories_dir=tmp_path)
    trace = _mock_trace("inc-trajtest")
    tracer.on_trace_start(trace)

    # Add a fake span to the internal buffer
    tracer._spans["trace_abc123"].append({"type": "function", "name": "fetch_logs"})

    tracer.on_trace_end(trace)
    assert (tmp_path / "inc-trajtest.json").exists()


def test_tracer_on_trace_end_no_dir_does_not_write(tmp_path: Path) -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm, trajectories_dir=None)
    trace = _mock_trace("inc-nowrite")
    tracer.on_trace_start(trace)
    tracer.on_trace_end(trace)
    # No files should be written anywhere
    assert list(tmp_path.iterdir()) == []


def test_tracer_on_trace_end_cleans_spans_dict() -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm)
    trace = _mock_trace("inc-cleanup")
    tracer.on_trace_start(trace)
    assert "trace_abc123" in tracer._spans
    tracer.on_trace_end(trace)
    assert "trace_abc123" not in tracer._spans


def test_tracer_shutdown_clears_state() -> None:
    stm = ShortTermMemory()
    tracer = SentinelTracer(stm)
    tracer._spans["trace_123"] = [{"type": "span"}]
    tracer.shutdown()
    assert len(tracer._spans) == 0


def test_tracer_multiple_incidents_isolated(tmp_path: Path) -> None:
    """Events from two concurrent traces must not cross-contaminate."""
    stm = ShortTermMemory()
    alert = IncidentAlert(title="m", severity="critical", service="svc")
    stm.create("inc-alpha001", alert)
    stm.create("inc-beta0001", alert)

    tracer = SentinelTracer(stm, trajectories_dir=tmp_path)

    # Simulate two separate traces
    trace_a = MagicMock()
    trace_a.trace_id = "trace_aaa"
    trace_a.metadata = {"incident_id": "inc-alpha001"}
    trace_a.export.return_value = {}

    trace_b = MagicMock()
    trace_b.trace_id = "trace_bbb"
    trace_b.metadata = {"incident_id": "inc-beta0001"}
    trace_b.export.return_value = {}

    tracer.on_trace_start(trace_a)
    tracer.on_trace_start(trace_b)

    # Tool call on trace A
    data_a = _function_span_data("fetch_logs", "logs for alpha")
    span_a = MagicMock()
    span_a.trace_id = "trace_aaa"
    span_a.span_data = data_a
    span_a.trace_metadata = {"incident_id": "inc-alpha001"}
    span_a.ended_at = None
    span_a.export.return_value = {"type": "function"}

    tracer.on_span_end(span_a)

    # Tool call on trace B
    data_b = _function_span_data("get_deploy_details", "deploys for beta")
    span_b = MagicMock()
    span_b.trace_id = "trace_bbb"
    span_b.span_data = data_b
    span_b.trace_metadata = {"incident_id": "inc-beta0001"}
    span_b.ended_at = None
    span_b.export.return_value = {"type": "function"}

    tracer.on_span_end(span_b)

    timeline_a = stm.get_timeline("inc-alpha001")
    timeline_b = stm.get_timeline("inc-beta0001")

    assert len(timeline_a) == 1
    assert timeline_a[0].action == "fetch_logs"
    assert len(timeline_b) == 1
    assert timeline_b[0].action == "get_deploy_details"


# ── LangFuse helpers (backend §6) ─────────────────────────────────────────────

_PROMPTS_DIR = Path(main.__file__).resolve().parent / "agents" / "prompts"


def _file_prompt(agent_name: str) -> str:
    """The prompt file's text with the untrusted-data preamble, as both loaders serve it."""
    raw = (_PROMPTS_DIR / f"{agent_name}.txt").read_text(encoding="utf-8").strip()
    return f"{raw}\n\n{UNTRUSTED_DATA_PREAMBLE}"


def _text_prompt(template: str, *, name: str = "triage", version: int = 3) -> TextPromptClient:
    """A real TextPromptClient, as ``get_prompt`` returns it from LangFuse."""
    return TextPromptClient(
        Prompt_Text(
            name=name, version=version, prompt=template, config={}, labels=["production"], tags=[]
        )
    )


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A stand-in for the LangFuse client returned by ``get_client()``."""
    client = MagicMock()
    monkeypatch.setattr(tracing, "get_client", lambda: client)
    return client


async def test_load_prompt_returns_langfuse_prompt(mock_client: MagicMock) -> None:
    prompt = _text_prompt("You are the Triage Agent for {{service}}.")
    mock_client.get_prompt.return_value = prompt

    result = await load_prompt("triage")

    assert isinstance(result, TextPromptClient)
    # Same LangFuse prompt and version (the §6.3 link), preamble appended to the template.
    assert (result.name, result.version) == ("triage", 3)
    assert result.compile(service="api") == (
        f"You are the Triage Agent for api.\n\n{UNTRUSTED_DATA_PREAMBLE}"
    )
    mock_client.get_prompt.assert_called_once_with("triage", cache_ttl_seconds=300)


async def test_langfuse_prompt_gets_the_preamble_once_across_cached_loads(
    mock_client: MagicMock,
) -> None:
    """The SDK hands back the same cached client each time: it must stay unchanged."""
    prompt = _text_prompt("You are the Triage Agent.")
    mock_client.get_prompt.return_value = prompt

    first = await load_prompt("triage")
    second = await load_prompt("triage")

    assert prompt.prompt == "You are the Triage Agent."
    for result in (first, second):
        assert isinstance(result, TextPromptClient)
        compiled = result.compile()
        assert compiled.endswith(UNTRUSTED_DATA_PREAMBLE)
        assert compiled.count(UNTRUSTED_DATA_PREAMBLE) == 1


async def test_langfuse_prompt_already_ending_with_the_preamble_is_not_doubled(
    mock_client: MagicMock,
) -> None:
    """A LangFuse prompt uploaded from the loader's output already carries the rule."""
    prompt = _text_prompt(_file_prompt("triage") + "\n")
    mock_client.get_prompt.return_value = prompt

    result = await load_prompt("triage")

    assert result is prompt
    assert result.compile().count(UNTRUSTED_DATA_PREAMBLE) == 1


async def test_non_text_langfuse_prompt_falls_back_to_the_file(mock_client: MagicMock) -> None:
    mock_client.get_prompt.return_value = MagicMock(name="ChatPromptClient")

    assert await load_prompt("triage") == _file_prompt("triage")


@pytest.mark.parametrize("agent_name", ["reflexion", "reverification"])
async def test_loop_prompts_load_with_one_preamble_on_both_paths(
    mock_client: MagicMock, agent_name: str
) -> None:
    """Decision 2026-10-14: the file loader and the LangFuse path serve the same text."""
    from_file_loader = loader.load_prompt(f"{agent_name}.txt")
    mock_client.get_prompt.side_effect = RuntimeError("langfuse unreachable")
    fallback = await load_prompt(agent_name)
    mock_client.get_prompt.side_effect = None
    raw = (_PROMPTS_DIR / f"{agent_name}.txt").read_text(encoding="utf-8")
    mock_client.get_prompt.return_value = _text_prompt(raw, name=agent_name)
    from_langfuse = await load_prompt(agent_name)

    assert isinstance(from_langfuse, TextPromptClient)
    for text in (from_file_loader, fallback, from_langfuse.compile()):
        assert text == _file_prompt(agent_name)
        assert text.endswith(UNTRUSTED_DATA_PREAMBLE)
        assert text.count(UNTRUSTED_DATA_PREAMBLE) == 1
        assert text.startswith("You are the ")


async def test_load_prompt_falls_back_to_file_when_get_prompt_raises(
    mock_client: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client.get_prompt.side_effect = RuntimeError("langfuse unreachable")
    monkeypatch.chdir(tmp_path)  # the fallback resolves against the package, not the cwd

    result = await load_prompt("triage")

    assert result == _file_prompt("triage")
    assert isinstance(result, str) and result.startswith("You are the Triage Agent")


async def test_load_prompt_raises_when_langfuse_and_file_both_fail(
    mock_client: MagicMock,
) -> None:
    mock_client.get_prompt.side_effect = RuntimeError("langfuse unreachable")

    with pytest.raises(FileNotFoundError):
        await load_prompt("no_such_agent")


async def test_load_prompt_fetches_off_the_event_loop_thread(mock_client: MagicMock) -> None:
    loop_thread = threading.get_ident()
    fetch_threads: list[int] = []

    def _get_prompt(*args: Any, **kwargs: Any) -> TextPromptClient:
        fetch_threads.append(threading.get_ident())
        return _text_prompt("You are the Triage Agent.")

    mock_client.get_prompt.side_effect = _get_prompt
    await load_prompt("triage")

    assert len(fetch_threads) == 1
    assert fetch_threads[0] != loop_thread


async def _ticks_during(coro: Any) -> tuple[Any, int]:
    """Run *coro* beside a 10 ms ticker; return its result and how often the ticker ran."""
    ticks = 0
    done = asyncio.Event()

    async def _ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(_ticker())
    try:
        result = await coro
    finally:
        done.set()
        await ticker
    return result, ticks


@pytest.mark.parametrize("unreachable", [False, True], ids=["slow", "unreachable"])
async def test_slow_langfuse_does_not_block_other_coroutines(
    mock_client: MagicMock, unreachable: bool
) -> None:
    prompt = _text_prompt("You are the Triage Agent.")

    def _blocking_get_prompt(*args: Any, **kwargs: Any) -> TextPromptClient:
        time.sleep(0.3)  # the SDK's synchronous HTTP call, retries included
        if unreachable:
            raise ConnectionError("connection refused")
        return prompt

    mock_client.get_prompt.side_effect = _blocking_get_prompt
    result, ticks = await _ticks_during(load_prompt("triage"))

    if unreachable:
        assert result == _file_prompt("triage")
    else:
        assert isinstance(result, TextPromptClient) and result.version == prompt.version
    # A blocked loop would let the ticker run once; ~30 ticks fit in 0.3 s.
    assert ticks >= 10


def test_push_score_calls_create_score_with_exact_kwargs(mock_client: MagicMock) -> None:
    push_score("0123456789abcdef0123456789abcdef", "triage_accuracy", 0.8)

    mock_client.create_score.assert_called_once_with(
        trace_id="0123456789abcdef0123456789abcdef",
        name="triage_accuracy",
        value=0.8,
        data_type="NUMERIC",
    )


def test_flush_and_shutdown_call_the_client(mock_client: MagicMock) -> None:
    flush_traces()
    shutdown_traces()

    mock_client.flush.assert_called_once_with()
    mock_client.shutdown.assert_called_once_with()


def test_flush_and_shutdown_never_raise(mock_client: MagicMock) -> None:
    mock_client.flush.side_effect = RuntimeError("export failed")
    mock_client.shutdown.side_effect = RuntimeError("export failed")

    flush_traces()
    shutdown_traces()


def test_incident_trace_id_is_uuid_hex() -> None:
    incident_id = uuid.uuid4()

    assert incident_trace_id(incident_id) == incident_id.hex
    assert incident_trace_id(str(incident_id)) == incident_id.hex
    trace_id = incident_trace_id(str(incident_id).upper())
    assert len(trace_id) == 32
    assert trace_id == trace_id.lower()
    assert all(c in "0123456789abcdef" for c in trace_id)
    with pytest.raises(ValueError):
        incident_trace_id("inc-not-a-uuid")


def test_trace_incident_opens_root_with_trace_id_and_attributes(
    mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    propagate = MagicMock()
    monkeypatch.setattr(tracing, "propagate_attributes", propagate)
    incident_id, correlation_id = uuid.uuid4(), uuid.uuid4()

    with trace_incident(incident_id, correlation_id, "sentinel-watchtower") as trace_id:
        assert trace_id == incident_id.hex
        mock_client.start_as_current_observation.return_value.__enter__.assert_called_once()
        propagate.return_value.__enter__.assert_called_once()

    mock_client.start_as_current_observation.assert_called_once_with(
        name=PIPELINE_TRACE_NAME, trace_context={"trace_id": incident_id.hex}
    )
    propagate.assert_called_once_with(
        metadata={"correlation_id": str(correlation_id)},
        tags=["incident", "sentinel-watchtower"],
    )
    mock_client.start_as_current_observation.return_value.__exit__.assert_called_once()


def test_trace_incident_truncates_attributes_to_200_chars(
    mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    propagate = MagicMock()
    monkeypatch.setattr(tracing, "propagate_attributes", propagate)

    with trace_incident(uuid.uuid4(), "c" * 300, "s" * 300):
        pass

    kwargs = propagate.call_args.kwargs
    assert kwargs["metadata"] == {"correlation_id": "c" * 200}
    assert kwargs["tags"] == ["incident", "s" * 200]


# ── observe: input/output capture is opt-in ────────────────────────────────────


@pytest.fixture
def observe_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The client langfuse's decorator resolves internally, as a mock."""
    from langfuse._client import observe as langfuse_observe_module  # noqa: PLC0415

    client = MagicMock()
    monkeypatch.setattr(langfuse_observe_module, "get_client", lambda **_: client)
    return client


def _span(client: MagicMock) -> MagicMock:
    return client.start_as_current_observation.return_value.__enter__.return_value


def _output_updates(client: MagicMock) -> list[Any]:
    return [c for c in _span(client).update.call_args_list if "output" in c.kwargs]


async def test_observe_does_not_capture_input_or_output_by_default(
    observe_client: MagicMock,
) -> None:
    @observe(name="fetch-logs", as_type="tool")
    async def fetch_logs(query: str) -> str:
        return f"raw log lines for {query}"

    assert await fetch_logs("payment-service") == "raw log lines for payment-service"

    kwargs = observe_client.start_as_current_observation.call_args.kwargs
    assert kwargs["name"] == "fetch-logs"
    assert kwargs["as_type"] == "tool"
    assert kwargs["input"] is None
    assert _output_updates(observe_client) == []


def test_bare_observe_does_not_capture_either(observe_client: MagicMock) -> None:
    @observe
    def summarise(text: str) -> str:
        return text.upper()

    assert summarise("prompt text") == "PROMPT TEXT"
    assert observe_client.start_as_current_observation.call_args.kwargs["input"] is None
    assert _output_updates(observe_client) == []


async def test_observe_captures_when_a_function_opts_in(observe_client: MagicMock) -> None:
    @observe(name="triage-agent", as_type="agent", capture_input=True, capture_output=True)
    async def run_triage(service: str) -> str:
        return f"P1 for {service}"

    await run_triage("payment-service")

    assert observe_client.start_as_current_observation.call_args.kwargs["input"] is not None
    assert [c.kwargs["output"] for c in _output_updates(observe_client)] == [
        "P1 for payment-service"
    ]


# ── No keys: the real (disabled) client — no crash, no network, no log noise ──


async def test_without_keys_everything_is_a_quiet_noop(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="langfuse")
    assert not langfuse_enabled()
    received: dict[str, Any] = {}

    @observe(name="fetch-logs", as_type="tool")
    def fetch_logs(query: str) -> str:
        return f"logs for {query}"

    @observe(name="triage-agent", as_type="agent")
    async def run_triage(service: str, **kwargs: Any) -> str:
        received.update(kwargs)
        return fetch_logs(service)

    incident_id = uuid.uuid4()
    with trace_incident(incident_id, uuid.uuid4(), "sentinel-watchtower") as trace_id:
        assert await run_triage("api") == "logs for api"
    assert trace_id == incident_id.hex
    # langfuse_trace_id is consumed by @observe, never passed through to the function.
    assert await run_triage("db", langfuse_trace_id=trace_id) == "logs for db"
    assert received == {}

    assert push_score(trace_id, "triage_accuracy", 1.0) is None
    prompt = await load_prompt("triage")  # disabled client raises in get_prompt → file
    assert prompt == _file_prompt("triage")
    flush_traces()
    shutdown_traces()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_blank_key_is_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "  ")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    assert not langfuse_enabled()

    assert configure_langfuse() is False

    assert "LANGFUSE_PUBLIC_KEY" not in os.environ
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-lf-test"


def test_langfuse_enabled_needs_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    assert not langfuse_enabled()
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    assert langfuse_enabled()


# ── Lifespan: the LangFuse client is shut down on exit ────────────────────────


@pytest.fixture
def light_lifespan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The real lifespan on a throwaway SQLite file, with app wiring stubbed out."""
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    wire = MagicMock()
    monkeypatch.setattr(main, "_wire_app", wire)
    return wire


async def test_lifespan_shuts_down_langfuse_on_exit(
    light_lifespan: MagicMock, mock_client: MagicMock
) -> None:
    loop_thread = threading.get_ident()
    shutdown_threads: list[int] = []
    mock_client.shutdown.side_effect = lambda: shutdown_threads.append(threading.get_ident())

    async with main.lifespan(FastAPI()):
        mock_client.shutdown.assert_not_called()

    mock_client.shutdown.assert_called_once_with()
    assert shutdown_threads != [loop_thread]  # flushed off the event loop


async def test_lifespan_closes_the_pool_when_shutdown_traces_is_cancelled(
    light_lifespan: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    close_pool = AsyncMock()
    monkeypatch.setattr(main, "close_pool", close_pool)
    real_to_thread = asyncio.to_thread

    async def _cancelled_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        if func is main.shutdown_traces:
            raise asyncio.CancelledError
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(main.asyncio, "to_thread", _cancelled_to_thread)

    with pytest.raises(asyncio.CancelledError):
        async with main.lifespan(FastAPI()):
            pass

    close_pool.assert_awaited_once_with()


async def test_lifespan_shuts_down_langfuse_after_failed_startup(
    light_lifespan: MagicMock, mock_client: MagicMock
) -> None:
    light_lifespan.side_effect = RuntimeError("wiring failed")

    with pytest.raises(RuntimeError, match="wiring failed"):
        async with main.lifespan(FastAPI()):
            pytest.fail("startup should not have completed")

    mock_client.shutdown.assert_called_once_with()
