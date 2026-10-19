"""Tests for sentinel.providers.fake — the deterministic, no-network fake LLM."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import litellm
import pytest
from agents import Agent, RunConfig, Runner, function_tool
from agents.items import ToolCallItem
from pydantic import BaseModel, Field

from sentinel.agents.loader import load_prompt
from sentinel.config import Settings
from sentinel.models.incident import Severity, TriageResult
from sentinel.models.reflexion import FetchLogsCall, ReflexionResult, SuspectDeploy
from sentinel.models.reverification import (
    UNPARSEABLE_REASON,
    ReverificationOutcome,
    parse_reverification,
)
from sentinel.models.tools import EscalationContext, LogFetchInput
from sentinel.providers import fake as fake_module
from sentinel.providers.fake import (
    REFLEXION_AGENT,
    REVERIFICATION_AGENT,
    FakeModel,
    FakeScriptError,
    FakeToolCall,
    FakeTurn,
    fake_script,
    reflexion_turn,
    reverification_turn,
)
from sentinel.providers.router import ROLES, call_with_fallback, model_for, model_name

HAIKU = "anthropic/claude-haiku-4-5"
NO_TRACING = RunConfig(tracing_disabled=True)


class Verdict(BaseModel):
    passed: bool
    confidence: float = Field(ge=0.5, le=1.0)
    reasons: list[str] = Field(min_length=1)


@pytest.fixture(autouse=True)
def no_network() -> Iterator[None]:
    """Every way out of the process raises: the fake model must never need one."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network access attempted")

    with (
        patch.object(socket.socket, "connect", refuse),
        patch.object(socket.socket, "connect_ex", refuse),
        patch.object(socket, "create_connection", refuse),
        patch.object(socket, "getaddrinfo", refuse),
    ):
        yield


def _fake_settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, sentinel_fake_llm=True, **kwargs)  # type: ignore[call-arg]


@contextmanager
def _generation_spans() -> Iterator[list[Any]]:
    """Capture every generation span the fake model opens."""
    spans: list[Any] = []
    real = fake_module.generation_span

    def spy(*args: Any, **kwargs: Any) -> Any:
        span = real(*args, **kwargs)
        spans.append(span)
        return span

    with patch.object(fake_module, "generation_span", spy):
        yield spans


# ── Fake mode in the router ───────────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_fake_mode_serves_every_role_without_keys(role: str) -> None:
    primary, fallback = model_for(role, _fake_settings())  # type: ignore[arg-type]
    assert isinstance(primary, FakeModel) and isinstance(fallback, FakeModel)


def test_fake_models_carry_the_section_4_7_strings() -> None:
    primary, fallback = model_for("orchestrator", _fake_settings())
    assert (model_name(primary), model_name(fallback)) == (  # type: ignore[arg-type]
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
    )


# ── Text and structured outputs ───────────────────────────────────────────────


async def test_default_text_output_is_deterministic() -> None:
    agent = Agent(name="pr_content_generator", instructions="write", model=FakeModel(HAIKU))
    with fake_script():
        first = await Runner.run(agent, "incident context")
        second = await Runner.run(agent, "incident context")
    assert first.final_output == second.final_output == "fake response"
    assert first.context_wrapper.usage.input_tokens == second.context_wrapper.usage.input_tokens


async def test_scripted_output_by_agent_name() -> None:
    agent = Agent(name="triage_agent", instructions="classify", model=FakeModel(HAIKU))
    with fake_script() as script:
        script.on("triage_agent", FakeTurn(output="P1 on api-gateway"))
        result = await Runner.run(agent, "alert", run_config=NO_TRACING)
    assert result.final_output == "P1 on api-gateway"
    assert script.calls == [("triage_agent", HAIKU)]


async def test_scripted_structured_output_matches_output_type() -> None:
    triage = TriageResult(
        severity=Severity.P1,
        affected_service="api-gateway",
        is_duplicate=False,
        recommended_action="analyse",
    )
    agent = Agent(
        name="triage_agent", instructions="x", model=FakeModel(HAIKU), output_type=TriageResult
    )
    with fake_script() as script:
        script.on("TriageResult", FakeTurn(output=triage))  # keyed by output type
        result = await Runner.run(agent, "alert")
    assert result.final_output == triage


async def test_unscripted_structured_output_is_schema_valid() -> None:
    agent = Agent(
        name="reverification", instructions="x", model=FakeModel(HAIKU), output_type=Verdict
    )
    with fake_script():
        result = await Runner.run(agent, "check")
    assert isinstance(result.final_output, Verdict)
    assert result.final_output.confidence == 0.5
    assert len(result.final_output.reasons) == 1


async def test_unscripted_sentinel_output_types_validate() -> None:
    for output_type in (TriageResult, EscalationContext):
        agent = Agent(name="a", instructions="x", model=FakeModel(HAIKU), output_type=output_type)
        with fake_script():
            result = await Runner.run(agent, "alert", run_config=NO_TRACING)
        assert isinstance(result.final_output, output_type)


async def test_constrained_string_needs_a_script() -> None:
    class Sha(BaseModel):
        commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")

    agent = Agent(name="resolution", instructions="x", model=FakeModel(HAIKU), output_type=Sha)
    with fake_script(), pytest.raises(FakeScriptError, match="script"):
        await Runner.run(agent, "x", run_config=NO_TRACING)


# ── Tool calls ────────────────────────────────────────────────────────────────


async def test_scripted_tool_calls_run_the_tools_then_finish() -> None:
    seen: list[str] = []

    @function_tool
    def get_service_metadata(service_name: str) -> str:
        """Look up a service."""
        seen.append(service_name)
        return f"{service_name}: tier=critical"

    agent = Agent(
        name="triage_agent",
        instructions="x",
        model=FakeModel(HAIKU),
        tools=[get_service_metadata],
    )
    with fake_script() as script:
        script.on(
            "triage_agent",
            FakeTurn(
                tool_calls=[
                    FakeToolCall("get_service_metadata", {"service_name": "api"}),
                    FakeToolCall("get_service_metadata", {"service_name": "db"}),
                ]
            ),
            FakeTurn(tool_calls=[FakeToolCall("get_service_metadata", {"service_name": "cache"})]),
            FakeTurn(output="done"),
        )
        result = await Runner.run(agent, "alert", run_config=NO_TRACING)

    assert seen == ["api", "db", "cache"]
    tool_calls = [item for item in result.new_items if isinstance(item, ToolCallItem)]
    assert len(tool_calls) == 3  # what 4.2's per-incident tool budget counts
    assert result.final_output == "done"
    assert result.context_wrapper.usage.requests == 3


# ── Usage ─────────────────────────────────────────────────────────────────────


async def test_scripted_usage_is_reported_on_the_run_and_the_generation_span() -> None:
    agent = Agent(name="triage_agent", instructions="x", model=FakeModel(HAIKU))
    with fake_script() as script, _generation_spans() as spans:
        script.on("triage_agent", FakeTurn(output="ok", usage=(1200, 80)))
        result = await Runner.run(agent, "alert")

    usage = result.context_wrapper.usage
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (1200, 80, 1280)
    assert len(spans) == 1
    data = spans[0].span_data
    assert data.model == HAIKU
    assert data.usage == {"input_tokens": 1200, "output_tokens": 80}
    assert data.input is None and data.output is None  # no data on the span


async def test_estimated_usage_grows_with_the_input() -> None:
    agent = Agent(name="a", instructions="x", model=FakeModel(HAIKU))
    with fake_script():
        short = await Runner.run(agent, "a", run_config=NO_TRACING)
        long = await Runner.run(agent, "a" * 4000, run_config=NO_TRACING)
    assert long.context_wrapper.usage.input_tokens > short.context_wrapper.usage.input_tokens
    assert short.context_wrapper.usage.output_tokens >= 1


# ── Errors and fallback ───────────────────────────────────────────────────────


async def test_scripted_failure_drives_the_router_fallback() -> None:
    agent = Agent(name="triage_agent", instructions="x")
    with fake_script() as script:
        script.fail_on(HAIKU, litellm.RateLimitError("429", llm_provider="anthropic", model=HAIKU))
        script.on("triage_agent", FakeTurn(output="from the fallback"))
        routed = await call_with_fallback(agent, "alert", role="triage", settings=_fake_settings())

    assert routed.model_used == "openai/gpt-4o-mini"
    assert routed.fell_back is True
    assert routed.result.final_output == "from the fallback"
    assert [model for _, model in script.calls] == [HAIKU, "openai/gpt-4o-mini"]


async def test_fake_model_does_not_stream() -> None:
    model = FakeModel(HAIKU)
    with pytest.raises(NotImplementedError):
        async for _ in model.stream_response(  # type: ignore[call-arg]
            None,
            "x",
            None,
            [],
            None,
            [],
            None,  # type: ignore[arg-type]
        ):
            pass


# ── Reflexion and reverification turns (backend §4.3, §4.4) ──────────────────

SONNET = "anthropic/claude-sonnet-4-6"
_LOGS_CALL = FetchLogsCall(
    tool="fetch_logs", args=LogFetchInput(service="sentinel-watchtower", window_minutes=30)
)


def _reflexion_agent() -> Agent[Any]:
    return Agent(
        name=REFLEXION_AGENT,
        instructions=load_prompt("reflexion.txt"),
        model=FakeModel(SONNET),
        output_type=ReflexionResult,
    )


def _reverification_agent() -> Agent[Any]:
    return Agent(
        name=REVERIFICATION_AGENT,
        instructions=load_prompt("reverification.txt"),
        model=FakeModel(SONNET),
    )


async def test_scripted_reflexion_turns_drive_a_loop_back_then_a_confident_result() -> None:
    deploy = SuspectDeploy(commit_sha="a1b2c3d", pr_number=42)
    with fake_script() as script:
        script.on(
            REFLEXION_AGENT,
            reflexion_turn(0.4, next_tool_call=_LOGS_CALL, missing_or_contradictory=["timing"]),
            reflexion_turn(0.85, deploy=deploy, supporting_evidence=["pr-42 before failure"]),
        )
        first = await Runner.run(_reflexion_agent(), "hypothesis + evidence", run_config=NO_TRACING)
        second = await Runner.run(
            _reflexion_agent(), "hypothesis + evidence", run_config=NO_TRACING
        )

    low, high = first.final_output, second.final_output
    assert isinstance(low, ReflexionResult) and isinstance(high, ReflexionResult)
    assert low.needs_more_evidence and low.next_tool_call == _LOGS_CALL
    assert low.missing_or_contradictory == ["timing"]
    assert not high.needs_more_evidence and high.next_tool_call is None
    assert high.specific_deploy_identified and high.deploy == deploy
    assert script.calls == [(REFLEXION_AGENT, SONNET)] * 2


def test_reflexion_turn_goes_through_the_contract_up_front() -> None:
    """The scripted reply is what ReflexionResult accepts: repaired, never rejected."""
    low = reflexion_turn(0.3).output
    assert isinstance(low, ReflexionResult)
    assert low.needs_more_evidence and low.next_tool_call is None
    high = reflexion_turn(1.5).output
    assert isinstance(high, ReflexionResult) and high.confidence == 1.0


async def test_unscripted_reflexion_is_low_confidence_and_valid() -> None:
    with fake_script():
        result = await Runner.run(_reflexion_agent(), "x", run_config=NO_TRACING)
    output = result.final_output
    assert isinstance(output, ReflexionResult)
    assert output.confidence == 0.0 and not output.specific_deploy_identified
    assert output.next_tool_call is not None and output.next_tool_call.tool == "fetch_logs"


@pytest.mark.parametrize(
    ("turn", "outcome", "reason"),
    [
        (reverification_turn("PASS"), ReverificationOutcome.PASS, None),
        (reverification_turn(ReverificationOutcome.PASS), ReverificationOutcome.PASS, None),
        (reverification_turn("FAIL", "wrong target"), ReverificationOutcome.FAIL, "wrong target"),
        (
            reverification_turn(ReverificationOutcome.ESCALATE, "too uncertain"),
            ReverificationOutcome.ESCALATE,
            "too uncertain",
        ),
        (
            reverification_turn("looks fine to me"),
            ReverificationOutcome.ESCALATE,
            UNPARSEABLE_REASON,
        ),
    ],
)
async def test_scripted_reverification_replies_parse(
    turn: FakeTurn, outcome: ReverificationOutcome, reason: str | None
) -> None:
    with fake_script() as script:
        script.on(REVERIFICATION_AGENT, turn)
        result = await Runner.run(_reverification_agent(), "fix + target", run_config=NO_TRACING)
    verdict = parse_reverification(result.final_output)
    assert (verdict.outcome, verdict.reason) == (outcome, reason)


async def test_unscripted_reverification_escalates() -> None:
    with fake_script():
        result = await Runner.run(_reverification_agent(), "x", run_config=NO_TRACING)
    verdict = parse_reverification(result.final_output)
    assert verdict.outcome is ReverificationOutcome.ESCALATE and verdict.parsed
