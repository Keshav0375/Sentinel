"""Tests for sentinel.agents.analysis — the merged log + deploy Analysis agent (§4.2).

Runs go through the fake LLM: the real tools execute (with no Datadog key and no database
they report "unavailable"), nothing reaches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from agents import Agent, Runner
from agents.extensions.models.litellm_model import LitellmModel

from sentinel.agents.analysis import ANALYSIS_AGENT_NAME, build_analysis_agent
from sentinel.config import Settings
from sentinel.models.analysis import AnalysisResult
from sentinel.models.deploy import CorrelatedDeploy
from sentinel.models.tools import DeployRecord, RollbackSpecInput, TargetDeploy
from sentinel.providers import FakeToolCall, FakeTurn, coerce_output, fake_script


@pytest.fixture()
def fake_llm() -> Settings:
    return Settings(_env_file=None, sentinel_fake_llm=True)  # type: ignore[call-arg]


@pytest.fixture()
def analysis_agent(fake_settings: Settings) -> Agent[object]:
    return build_analysis_agent(None, settings=fake_settings)


def _suspect() -> CorrelatedDeploy:
    return CorrelatedDeploy(
        pr_number=42,
        commit_sha="a1b2c3d",
        deployed_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
        version="pr-42-a1b2c3d",
        deploy_status="succeeded",
        failed_stage="none",
    )


# ── AnalysisResult ────────────────────────────────────────────────────────────


def test_analysis_result_minimal() -> None:
    result = AnalysisResult(hypothesis="GET / returns HTML since pr-42")
    assert result.evidence == [] and result.error_patterns == []
    assert result.suspect_deploy is None


def test_analysis_result_round_trips_as_plain_text_json() -> None:
    """Anthropic/OpenAI agents answer in text: the JSON must coerce back unchanged."""
    original = AnalysisResult(
        hypothesis="h", evidence=["fetch_logs: 3 failed runs"], suspect_deploy=_suspect()
    )
    assert coerce_output(original.model_dump_json(), AnalysisResult) == original


def test_suspect_deploy_is_a_deploy_record_subset_that_feeds_the_rollback_spec() -> None:
    record = DeployRecord(
        pr_number=42,
        commit_sha="a1b2c3d",
        deploy_status="failed",
        deployed_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
        failed_stage="verify",
        version="pr-42-a1b2c3d",
    )
    suspect = CorrelatedDeploy.model_validate(record.model_dump())
    result = AnalysisResult(hypothesis="h", suspect_deploy=suspect)
    assert result.suspect_deploy is not None
    target = TargetDeploy.model_validate(result.suspect_deploy.model_dump())
    spec_input = RollbackSpecInput(
        incident_id="i",
        root_cause="r",
        evidence_summary="e",
        confidence=0.9,
        justification="j",
        target_deploy=target,
    )
    assert spec_input.target_deploy.commit_sha == record.commit_sha
    assert spec_input.target_deploy.deployed_at == record.deployed_at


# ── build_analysis_agent ──────────────────────────────────────────────────────


def test_agent_name_is_the_prompt_name(analysis_agent: Agent[object]) -> None:
    assert ANALYSIS_AGENT_NAME == "analysis"
    assert analysis_agent.name == "analysis"


def test_agent_has_exactly_the_two_analysis_tools(analysis_agent: Agent[object]) -> None:
    assert sorted(t.name for t in analysis_agent.tools) == ["fetch_logs", "get_deploy_details"]


def test_agent_has_no_handoffs_and_plain_text_output(analysis_agent: Agent[object]) -> None:
    assert analysis_agent.handoffs == []
    # Neither Anthropic nor OpenAI-with-fallback supports structured outputs on both sides.
    assert analysis_agent.output_type is None


def test_agent_runs_on_the_analysis_role_models(fake_settings: Settings) -> None:
    agent = build_analysis_agent(None, settings=fake_settings)
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "anthropic/claude-sonnet-4-6"
    openai = fake_settings.model_copy(update={"sentinel_primary_provider": "openai"})
    agent = build_analysis_agent(None, settings=openai)
    assert isinstance(agent.model, LitellmModel) and agent.model.model == "openai/gpt-4o"


def test_instructions_default_to_the_prompt_file(analysis_agent: Agent[object]) -> None:
    assert isinstance(analysis_agent.instructions, str)
    assert analysis_agent.instructions.startswith("You are the Analysis Agent")


def test_instructions_can_be_injected(fake_settings: Settings) -> None:
    agent = build_analysis_agent(None, settings=fake_settings, instructions="compiled prompt")
    assert agent.instructions == "compiled prompt"


async def test_agent_calls_both_tools_and_answers_with_an_analysis(fake_llm: Settings) -> None:
    """Both tools run (and say "unavailable" with no key and no pool); the reply coerces."""
    agent = build_analysis_agent(None, settings=fake_llm)
    answer = AnalysisResult(hypothesis="no evidence could be read", evidence=["both failed"])
    with fake_script() as script:
        script.on(
            ANALYSIS_AGENT_NAME,
            FakeTurn(
                tool_calls=[
                    FakeToolCall("fetch_logs", {"request": {"service": "sentinel-watchtower"}}),
                    FakeToolCall(
                        "get_deploy_details", {"request": {"service": "sentinel-watchtower"}}
                    ),
                ]
            ),
            FakeTurn(output=answer),
        )
        result = await Runner.run(agent, "alert")

    outputs = [json.loads(str(item.output)) for item in result.new_items if hasattr(item, "output")]
    assert {o["tool"] for o in outputs} == {"fetch_logs", "get_deploy_details"}
    assert all(o["error"] is True and o["kind"] == "unavailable" for o in outputs)
    assert coerce_output(str(result.final_output), AnalysisResult) == answer
