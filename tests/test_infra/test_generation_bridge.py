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


def test_register_is_add_only_and_once(monkeypatch: pytest.MonkeyPatch) -> None:
    add = MagicMock(name="add_trace_processor")
    monkeypatch.setattr(tracing, "add_trace_processor", add)
    monkeypatch.setattr(tracing, "_bridge", None)
    first = tracing.register_generation_bridge()
    second = tracing.register_generation_bridge()
    assert first is second
    add.assert_called_once_with(first)


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
    """End to end: a FakeModel run's generation spans, fed to the bridge, sum per incident."""
    from agents import Agent, Runner  # noqa: PLC0415

    from sentinel.providers import fake as fake_module  # noqa: PLC0415
    from sentinel.providers.fake import FakeModel, FakeTurn, fake_script  # noqa: PLC0415

    monkeypatch.setattr(tracing, "langfuse_enabled", lambda: False)
    bridge = LangfuseGenerationBridge()
    real_span = fake_module.generation_span

    class _Bridged:
        """Wraps the fake model's generation span so the bridge sees its start and end."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._span = real_span(*args, **kwargs)

        def __enter__(self) -> Any:
            span = self._span.__enter__()
            bridge.on_span_start(span)
            return span

        def __exit__(self, *exc: Any) -> Any:
            result = self._span.__exit__(*exc)
            bridge.on_span_end(self._span)
            return result

    monkeypatch.setattr(fake_module, "generation_span", _Bridged)
    agent = Agent(name="triage_agent", instructions="x", model=FakeModel(HAIKU))
    with fake_script() as script, track_incident_tokens("inc-fake") as tokens:
        script.on("triage_agent", FakeTurn(output="a", usage=(30_000, 1_000)))
        await Runner.run(agent, "alert")
        script.on("triage_agent", FakeTurn(output="b", usage=(25_000, 500)))
        await Runner.run(agent, "alert")
    assert tokens.total_tokens == 56_500
