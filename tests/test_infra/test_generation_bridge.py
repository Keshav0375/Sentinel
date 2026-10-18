"""Tests for the R14 token bridge — generation spans → LangFuse, per-incident totals."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from agents import AgentSpanData, FunctionSpanData, GenerationSpanData

from sentinel.infra import tracing
from sentinel.infra.tracing import (
    INCIDENT_TOKEN_BUDGET,
    LangfuseGenerationBridge,
    current_incident_tokens,
    track_incident_tokens,
    use_agent_prompts,
)

HAIKU = "anthropic/claude-haiku-4-5"


def _span(span_id: str, data: Any, parent_id: str | None = None, error: Any = None) -> Any:
    return SimpleNamespace(span_id=span_id, parent_id=parent_id, span_data=data, error=error)


def _generation(span_id: str = "gen-1", usage: dict[str, int] | None = None) -> Any:
    data = GenerationSpanData(
        model=HAIKU,
        input=[{"role": "user", "content": "raw logs: SECRET"}],  # must never be exported
        output=[{"role": "assistant", "content": "SECRET"}],
        usage=usage if usage is not None else {"input_tokens": 1200, "output_tokens": 80},
    )
    return _span(span_id, data, parent_id="agent-1")


def _run_one_generation(bridge: LangfuseGenerationBridge, gen: Any) -> None:
    agent = _span("agent-1", AgentSpanData(name="triage_agent"))
    bridge.on_span_start(agent)
    bridge.on_span_start(gen)
    bridge.on_span_end(gen)
    bridge.on_span_end(agent)


@pytest.fixture
def langfuse_on(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock(name="langfuse_client")
    monkeypatch.setattr(tracing, "langfuse_enabled", lambda: True)
    monkeypatch.setattr(tracing, "get_client", lambda: client)
    return client


@pytest.fixture
def tracing_log() -> Iterator[MagicMock]:
    with patch.object(tracing, "logger") as log:
        yield log


# ── LangFuse export ───────────────────────────────────────────────────────────


def test_generation_becomes_a_langfuse_generation_without_io(langfuse_on: MagicMock) -> None:
    _run_one_generation(LangfuseGenerationBridge(), _generation())

    langfuse_on.start_observation.assert_called_once_with(
        as_type="generation", name="triage_agent", model=HAIKU
    )
    observation = langfuse_on.start_observation.return_value
    observation.update.assert_called_once_with(usage_details={"input": 1200, "output": 80})
    observation.end.assert_called_once_with()
    for call in (langfuse_on.start_observation.call_args, observation.update.call_args):
        assert "input" not in call.kwargs and "output" not in call.kwargs
        assert "SECRET" not in repr(call)


def test_registered_prompt_is_linked(langfuse_on: MagicMock) -> None:
    prompt = MagicMock(name="TextPromptClient")
    with use_agent_prompts({"triage_agent": prompt}):
        _run_one_generation(LangfuseGenerationBridge(), _generation())

    observation = langfuse_on.start_observation.return_value
    observation.update.assert_called_once_with(
        usage_details={"input": 1200, "output": 80}, prompt=prompt
    )


def test_prompt_of_another_agent_is_not_linked(langfuse_on: MagicMock) -> None:
    with use_agent_prompts({"analysis_agent": MagicMock()}):
        _run_one_generation(LangfuseGenerationBridge(), _generation())
    assert "prompt" not in langfuse_on.start_observation.return_value.update.call_args.kwargs


def test_failed_generation_is_marked_error(langfuse_on: MagicMock) -> None:
    gen = _generation()
    gen.error = {"message": "rate limited", "data": None}
    _run_one_generation(LangfuseGenerationBridge(), gen)
    update = langfuse_on.start_observation.return_value.update.call_args.kwargs
    assert update["level"] == "ERROR"


def test_other_spans_are_ignored(langfuse_on: MagicMock) -> None:
    bridge = LangfuseGenerationBridge()
    tool = _span("tool-1", FunctionSpanData(name="fetch_logs", input="{}", output="logs"))
    bridge.on_span_start(tool)
    bridge.on_span_end(tool)
    langfuse_on.start_observation.assert_not_called()


def test_langfuse_failure_never_reaches_the_run(
    langfuse_on: MagicMock, tracing_log: MagicMock
) -> None:
    langfuse_on.start_observation.side_effect = RuntimeError("langfuse down")
    with track_incident_tokens("inc-1") as tokens:
        _run_one_generation(LangfuseGenerationBridge(), _generation())
    assert tokens.total_tokens == 1280  # usage still counted
    tracing_log.warning.assert_called_once()
    assert tracing_log.warning.call_args.args[0] == "langfuse_generation_failed"


def test_disabled_langfuse_makes_no_client_call(monkeypatch: pytest.MonkeyPatch) -> None:
    get_client = MagicMock(name="get_client")
    monkeypatch.setattr(tracing, "langfuse_enabled", lambda: False)
    monkeypatch.setattr(tracing, "get_client", get_client)

    with track_incident_tokens("inc-1") as tokens:
        _run_one_generation(LangfuseGenerationBridge(), _generation())

    get_client.assert_not_called()
    assert (tokens.input_tokens, tokens.output_tokens) == (1200, 80)


def test_install_sets_exactly_our_processors_and_drops_the_openai_exporter() -> None:
    """After startup wiring the processor list is ours alone, the bridge last."""
    from agents.tracing import get_trace_provider  # noqa: PLC0415
    from agents.tracing.processors import BatchTraceProcessor  # noqa: PLC0415

    provider: Any = get_trace_provider()
    multi = provider._multi_processor  # pyright: ignore[reportPrivateUsage]
    before = list(multi._processors)  # pyright: ignore[reportPrivateUsage]
    ours = [MagicMock(name="SentinelTracer"), MagicMock(name="DashboardEventEmitter")]
    try:
        multi.set_processors([BatchTraceProcessor(MagicMock())])  # the SDK default's kind
        installed = tracing.install_trace_processors(*ours)
        current = list(multi._processors)  # pyright: ignore[reportPrivateUsage]
        tracing.install_trace_processors(*ours)  # a second setup (e.g. app re-created)
        again = list(multi._processors)  # pyright: ignore[reportPrivateUsage]
    finally:
        multi.set_processors(before)

    assert current == installed == [*ours, tracing.generation_bridge()]
    assert not any(isinstance(p, BatchTraceProcessor) for p in current)
    assert again == current  # replaced, not appended: no duplicates
    assert tracing.generation_bridge() is tracing.generation_bridge()


async def test_litellm_generation_spans_carry_no_input_or_output() -> None:
    """Through call_with_fallback the real LitellmModel records usage, never data."""
    import litellm  # noqa: PLC0415
    from agents import Agent  # noqa: PLC0415
    from agents.extensions.models import litellm_model  # noqa: PLC0415

    from sentinel.config import Settings  # noqa: PLC0415
    from sentinel.providers.router import call_with_fallback  # noqa: PLC0415

    spans: list[Any] = []
    real_span = litellm_model.generation_span

    def spy(*args: Any, **kwargs: Any) -> Any:
        span = real_span(*args, **kwargs)
        spans.append(span)
        return span

    response = litellm.ModelResponse(
        model="claude-haiku-4-5",
        choices=[{"index": 0, "message": {"role": "assistant", "content": "SECRET out"}}],
        usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    )
    settings = Settings(_env_file=None, anthropic_api_key="sk-ant-x", openai_api_key="sk-x")  # type: ignore[call-arg]
    with (
        patch.object(litellm_model, "generation_span", spy),
        patch.object(litellm, "acompletion", return_value=response) as acompletion,
    ):
        routed = await call_with_fallback(
            Agent(name="triage_agent", instructions="SECRET instructions"),
            "SECRET logs",
            role="triage",
            settings=settings,
        )

    acompletion.assert_awaited_once()
    assert routed.result.final_output == "SECRET out"
    assert len(spans) == 1
    data = spans[0].span_data
    assert data.input is None and data.output is None
    assert data.usage == {"input_tokens": 12, "output_tokens": 3}
    assert data.model == HAIKU


async def test_function_and_generation_spans_carry_no_input_or_output() -> None:
    """A real agent loop (fake model + a tool) through call_with_fallback: the processors
    see the tool call and the generations, but no arguments, results, prompts or replies."""
    from agents import Agent, Span, TracingProcessor, function_tool  # noqa: PLC0415
    from agents.tracing import get_trace_provider  # noqa: PLC0415

    from sentinel.config import Settings  # noqa: PLC0415
    from sentinel.providers.fake import FakeToolCall, FakeTurn, fake_script  # noqa: PLC0415
    from sentinel.providers.router import call_with_fallback  # noqa: PLC0415

    class _Recorder(TracingProcessor):
        def __init__(self) -> None:
            self.spans: list[Span[Any]] = []

        def on_trace_start(self, trace: Any) -> None: ...
        def on_trace_end(self, trace: Any) -> None: ...
        def on_span_start(self, span: Span[Any]) -> None: ...

        def on_span_end(self, span: Span[Any]) -> None:
            self.spans.append(span)

        def shutdown(self) -> None: ...
        def force_flush(self) -> None: ...

    @function_tool
    def get_logs(service_name: str) -> str:
        """Return the service's logs."""
        return f"SECRET log line for {service_name}"

    recorder = _Recorder()
    multi: Any = get_trace_provider()._multi_processor  # type: ignore[attr-defined]
    before = list(multi._processors)
    settings = Settings(_env_file=None, sentinel_fake_llm=True)  # type: ignore[call-arg]
    agent = Agent(name="triage_agent", instructions="SECRET instructions", tools=[get_logs])
    try:
        tracing.install_trace_processors(recorder)
        with fake_script() as script:
            script.on(
                "triage_agent",
                FakeTurn(tool_calls=[FakeToolCall("get_logs", {"service_name": "SECRET-api"})]),
                FakeTurn(output="SECRET verdict"),
            )
            routed = await call_with_fallback(
                agent, "SECRET alert", role="triage", settings=settings
            )
    finally:
        multi.set_processors(before)

    assert routed.result.final_output == "SECRET verdict"
    functions = [s.span_data for s in recorder.spans if isinstance(s.span_data, FunctionSpanData)]
    generations = [
        s.span_data for s in recorder.spans if isinstance(s.span_data, GenerationSpanData)
    ]
    assert [f.name for f in functions] == ["get_logs"]
    assert len(generations) == 2
    for data in (*functions, *generations):
        assert data.input is None and data.output is None
    assert all(g.usage for g in generations)  # usage still flows to the bridge
    assert "SECRET" not in repr([s.export() for s in recorder.spans])


def test_requests_to_openai_are_blocked_in_tests() -> None:
    """The suite-wide guard: nothing (the trace exporter included) reaches api.openai.com."""
    import httpx  # noqa: PLC0415

    # conftest's OpenAIRequestBlocked (an AssertionError; conftest is not importable).
    with httpx.Client() as client, pytest.raises(AssertionError, match="api.openai.com"):
        client.post("https://api.openai.com/v1/traces/ingest", json={})


# ── Per-incident token accumulator ────────────────────────────────────────────


def test_tokens_sum_across_generations_and_only_inside_the_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracing, "langfuse_enabled", lambda: False)
    bridge = LangfuseGenerationBridge()
    assert current_incident_tokens() is None
    with track_incident_tokens("inc-1") as tokens:
        assert current_incident_tokens() is tokens
        _run_one_generation(bridge, _generation("g1", {"input_tokens": 100, "output_tokens": 10}))
        _run_one_generation(bridge, _generation("g2", {"input_tokens": 200, "output_tokens": 20}))
        _run_one_generation(bridge, _generation("g3", {}))  # no usage reported
    _run_one_generation(bridge, _generation("g4"))  # outside any incident: not counted
    assert (tokens.input_tokens, tokens.output_tokens, tokens.total_tokens) == (300, 30, 330)
    assert current_incident_tokens() is None


def test_budget_warning_fires_once_past_50k(tracing_log: MagicMock) -> None:
    assert INCIDENT_TOKEN_BUDGET == 50_000
    with track_incident_tokens("inc-big") as tokens:
        tokens.add(40_000, 10_000)  # exactly 50K: not exceeded
        tracing_log.warning.assert_not_called()
        tokens.add(1, 0)
        tokens.add(5_000, 0)
    tracing_log.warning.assert_called_once_with(
        "incident_token_budget_exceeded",
        incident_id="inc-big",
        total_tokens=50_001,
        input_tokens=40_001,
        output_tokens=10_000,
        budget=50_000,
    )


async def test_fake_llm_usage_flows_into_the_incident_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the installed bridge sums a FakeModel run's usage per incident."""
    from agents import Agent, Runner  # noqa: PLC0415
    from agents.tracing import get_trace_provider  # noqa: PLC0415

    from sentinel.providers.fake import FakeModel, FakeTurn, fake_script  # noqa: PLC0415

    monkeypatch.setattr(tracing, "langfuse_enabled", lambda: False)
    multi: Any = get_trace_provider()._multi_processor  # type: ignore[attr-defined]
    before = list(multi._processors)
    agent = Agent(name="triage_agent", instructions="x", model=FakeModel(HAIKU))
    try:
        tracing.install_trace_processors()  # the bridge alone
        with fake_script() as script, track_incident_tokens("inc-fake") as tokens:
            script.on("triage_agent", FakeTurn(output="a", usage=(30_000, 1_000)))
            await Runner.run(agent, "alert")
            script.on("triage_agent", FakeTurn(output="b", usage=(25_000, 500)))
            await Runner.run(agent, "alert")
    finally:
        multi.set_processors(before)
    assert tokens.total_tokens == 56_500
