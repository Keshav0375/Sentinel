"""Tests for sentinel.agents.deploy_correlator — DeployCorrelation model and agent structure."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from agents import Agent
from agents.extensions.models.litellm_model import LitellmModel

from sentinel.agents.deploy_correlator import (
    DEPLOY_CORRELATOR_AGENT_NAME,
    build_deploy_correlator_agent,
)
from sentinel.config import Settings
from sentinel.generator.alert_gen import load_scenario
from sentinel.generator.scenarios import Scenario
from sentinel.models.deploy import Deploy, DeployCorrelation

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def scenario() -> Scenario:
    return load_scenario("bad_deploy_01")


@pytest.fixture()
def correlator_agent(
    scenario: Scenario,
    fake_settings: Settings,
) -> Agent[DeployCorrelation]:
    return build_deploy_correlator_agent(
        scenario,
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )


@pytest.fixture()
def sample_deploy() -> Deploy:
    return Deploy(
        id="deploy-abc123",
        service="api-gateway",
        timestamp=datetime(2026, 5, 10, 2, 45, 0, tzinfo=UTC),
        author="dev-bot",
        commit_sha="a1b2c3d",
        files_changed=3,
        description="Update AuthMiddleware config handling",
    )


@pytest.fixture()
def innocent_deploy() -> Deploy:
    return Deploy(
        id="deploy-xyz789",
        service="api-gateway",
        timestamp=datetime(2026, 5, 9, 14, 0, 0, tzinfo=UTC),
        author="keshav",
        commit_sha="x7y8z9a",
        files_changed=1,
        description="Fix typo in error message",
    )


# ── DeployCorrelation model ───────────────────────────────────────────────────


def test_deploy_correlation_no_suspect(sample_deploy: Deploy) -> None:
    """Correlation with no culprit — downstream outage scenario."""
    corr = DeployCorrelation(
        recent_deploys=[sample_deploy],
        suspect_deploy=None,
        confidence=0.0,
        evidence="No deploy found within 2 hours of the incident.",
    )
    assert corr.suspect_deploy is None
    assert corr.confidence == 0.0
    assert len(corr.recent_deploys) == 1


def test_deploy_correlation_with_suspect(sample_deploy: Deploy) -> None:
    corr = DeployCorrelation(
        recent_deploys=[sample_deploy],
        suspect_deploy=sample_deploy,
        confidence=0.85,
        evidence="deploy-abc123 modified AuthMiddleware 15 minutes before first error.",
    )
    assert corr.suspect_deploy is not None
    assert corr.suspect_deploy.id == "deploy-abc123"
    assert corr.confidence == 0.85


def test_deploy_correlation_multiple_deploys(
    sample_deploy: Deploy, innocent_deploy: Deploy
) -> None:
    corr = DeployCorrelation(
        recent_deploys=[sample_deploy, innocent_deploy],
        suspect_deploy=sample_deploy,
        confidence=0.9,
        evidence="deploy-abc123 closest in time and touches auth path.",
    )
    assert len(corr.recent_deploys) == 2
    assert corr.suspect_deploy == sample_deploy


def test_deploy_correlation_default_confidence() -> None:
    corr = DeployCorrelation(evidence="No deploys found.")
    assert corr.confidence == 0.0
    assert corr.recent_deploys == []
    assert corr.suspect_deploy is None


def test_deploy_correlation_requires_evidence() -> None:
    with pytest.raises(Exception):
        DeployCorrelation.model_validate({"confidence": 0.5})


def test_deploy_correlation_json_roundtrip(sample_deploy: Deploy) -> None:
    original = DeployCorrelation(
        recent_deploys=[sample_deploy],
        suspect_deploy=sample_deploy,
        confidence=0.75,
        evidence="Timing and file scope match the error logs.",
    )
    restored = DeployCorrelation.model_validate_json(original.model_dump_json())
    assert restored.confidence == original.confidence
    assert restored.suspect_deploy is not None
    assert restored.suspect_deploy.id == sample_deploy.id
    assert len(restored.recent_deploys) == 1


def test_deploy_correlation_serialization(sample_deploy: Deploy) -> None:
    corr = DeployCorrelation(
        recent_deploys=[sample_deploy],
        suspect_deploy=sample_deploy,
        confidence=0.8,
        evidence="deploy-abc123 is the suspect.",
    )
    data = corr.model_dump()
    assert data["confidence"] == 0.8
    assert data["suspect_deploy"] is not None
    assert data["suspect_deploy"]["id"] == "deploy-abc123"


def test_deploy_correlation_confidence_range_zero() -> None:
    corr = DeployCorrelation(confidence=0.0, evidence="No correlation.")
    assert corr.confidence == 0.0


def test_deploy_correlation_confidence_range_one(sample_deploy: Deploy) -> None:
    corr = DeployCorrelation(
        recent_deploys=[sample_deploy],
        suspect_deploy=sample_deploy,
        confidence=1.0,
        evidence="Certain culprit — module and timing match exactly.",
    )
    assert corr.confidence == 1.0


# ── build_deploy_correlator_agent — structural tests ──────────────────────────


def test_build_deploy_correlator_agent_returns_agent(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert isinstance(correlator_agent, Agent)


def test_deploy_correlator_agent_name(correlator_agent: Agent[DeployCorrelation]) -> None:
    assert correlator_agent.name == DEPLOY_CORRELATOR_AGENT_NAME


def test_deploy_correlator_agent_name_constant() -> None:
    assert DEPLOY_CORRELATOR_AGENT_NAME == "deploy_correlator_agent"


def test_deploy_correlator_agent_has_one_tool(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    """Deploy correlator has exactly one tool — list_recent_deploys."""
    assert len(correlator_agent.tools) == 1


def test_deploy_correlator_agent_tool_name(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert correlator_agent.tools[0].name == "list_recent_deploys"


def test_deploy_correlator_agent_no_extra_tools(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    names = {t.name for t in correlator_agent.tools}
    unexpected = {
        "fetch_logs",
        "get_service_metadata",
        "search_past_incidents",
        "draft_rollback_pr",
        "request_human_approval",
    }
    assert not names & unexpected, f"Unexpected tools: {names & unexpected}"


def test_deploy_correlator_agent_output_type(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert correlator_agent.output_type is None


def test_deploy_correlator_agent_has_instructions(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    instructions = correlator_agent.instructions
    assert isinstance(instructions, str) and len(instructions) > 0


def test_deploy_correlator_agent_instructions_mention_confidence(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert isinstance(correlator_agent.instructions, str)
    assert "confidence" in correlator_agent.instructions.lower()


def test_deploy_correlator_agent_instructions_mention_list_recent_deploys(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert isinstance(correlator_agent.instructions, str)
    assert "list_recent_deploys" in correlator_agent.instructions


def test_deploy_correlator_agent_uses_litellm_model(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert isinstance(correlator_agent.model, LitellmModel)


def test_deploy_correlator_agent_model_string_forwarded(
    scenario: Scenario,
    fake_settings: Settings,
) -> None:
    agent = build_deploy_correlator_agent(
        scenario,
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "groq/llama-3.1-8b-instant"


def test_deploy_correlator_agent_custom_model_string(
    scenario: Scenario,
    fake_settings: Settings,
) -> None:
    agent = build_deploy_correlator_agent(
        scenario,
        model_string="groq/llama-3.3-70b-versatile",
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "groq/llama-3.3-70b-versatile"


def test_deploy_correlator_agent_no_handoffs(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert len(correlator_agent.handoffs) == 0


def test_deploy_correlator_agent_accepts_none_scenario(fake_settings: Settings) -> None:
    """Agent constructs with None scenario; list_recent_deploys errors at call time."""
    agent = build_deploy_correlator_agent(
        None,
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )
    assert isinstance(agent, Agent)
    assert len(agent.tools) == 1


def test_deploy_correlator_agent_mock_scenario(fake_settings: Settings) -> None:
    """Agent constructs with a MagicMock scenario."""
    mock_scenario = MagicMock(spec=Scenario)
    agent = build_deploy_correlator_agent(
        mock_scenario,
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )
    assert isinstance(agent, Agent)
