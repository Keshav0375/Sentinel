"""Tests for sentinel.agents.log_analyst — agent structure and LogAnalysis output type."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agents import Agent
from agents.extensions.models.litellm_model import LitellmModel

from data.seed import seed
from sentinel.agents.log_analyst import LOG_ANALYST_AGENT_NAME, build_log_analyst_agent
from sentinel.config import Settings
from sentinel.generator.alert_gen import load_scenario
from sentinel.generator.scenarios import Scenario
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.log_entry import LogAnalysis, LogEntry, LogLevel

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test_log_analyst.db"
    await seed(path, verbose=False)
    return path


@pytest.fixture()
def semantic_memory(db_path: Path) -> SemanticMemory:
    return SemanticMemory(db_path)


@pytest.fixture()
def scenario() -> Scenario:
    return load_scenario("bad_deploy_01")


@pytest.fixture()
def log_analyst_agent(
    semantic_memory: SemanticMemory,
    scenario: Scenario,
    fake_settings: Settings,
) -> Agent[LogAnalysis]:
    return build_log_analyst_agent(
        semantic_memory,
        scenario,
        model_string="groq/llama-3.3-70b-versatile",
        settings=fake_settings,
    )


# ── LogAnalysis output model ──────────────────────────────────────────────────


def test_log_analysis_minimal_construction() -> None:
    analysis = LogAnalysis(
        anomaly_summary="Error rate spiked at 03:00 UTC",
        hypothesis="Null pointer in AuthMiddleware introduced by deploy-abc123",
    )
    assert analysis.error_patterns == []
    assert analysis.key_log_lines == []
    assert "03:00" in analysis.anomaly_summary


def test_log_analysis_with_patterns_and_lines() -> None:
    entry = LogEntry(
        level=LogLevel.ERROR,
        service="api-gateway",
        message="NullPointerException in AuthMiddleware.validate()",
    )
    analysis = LogAnalysis(
        error_patterns=["NullPointerException in AuthMiddleware.validate()"],
        anomaly_summary="Repeated NPE starting at 03:00 UTC",
        key_log_lines=[entry],
        hypothesis="AuthMiddleware fails on null config key",
    )
    assert len(analysis.error_patterns) == 1
    assert len(analysis.key_log_lines) == 1
    assert analysis.key_log_lines[0].service == "api-gateway"


def test_log_analysis_json_roundtrip() -> None:
    entry = LogEntry(
        level=LogLevel.CRITICAL,
        service="payment-service",
        message="Connection refused: db-primary:5432",
    )
    original = LogAnalysis(
        error_patterns=["Connection refused: db-primary:5432"],
        anomaly_summary="DB connectivity lost at 14:22 UTC",
        key_log_lines=[entry],
        hypothesis="Primary database went offline",
    )
    restored = LogAnalysis.model_validate_json(original.model_dump_json())
    assert restored.hypothesis == original.hypothesis
    assert len(restored.key_log_lines) == 1
    assert restored.key_log_lines[0].message == entry.message


def test_log_analysis_requires_anomaly_summary() -> None:
    with pytest.raises(Exception):
        LogAnalysis.model_validate({"hypothesis": "something"})


def test_log_analysis_requires_hypothesis() -> None:
    with pytest.raises(Exception):
        LogAnalysis.model_validate({"anomaly_summary": "something"})


# ── build_log_analyst_agent — structural tests ────────────────────────────────


def test_build_log_analyst_agent_returns_agent(
    log_analyst_agent: Agent[LogAnalysis],
) -> None:
    assert isinstance(log_analyst_agent, Agent)


def test_log_analyst_agent_name(log_analyst_agent: Agent[LogAnalysis]) -> None:
    assert log_analyst_agent.name == LOG_ANALYST_AGENT_NAME


def test_log_analyst_agent_name_constant() -> None:
    assert LOG_ANALYST_AGENT_NAME == "log_analyst_agent"


def test_log_analyst_agent_has_two_tools(log_analyst_agent: Agent[LogAnalysis]) -> None:
    assert len(log_analyst_agent.tools) == 2


def test_log_analyst_agent_tool_names(log_analyst_agent: Agent[LogAnalysis]) -> None:
    names = {t.name for t in log_analyst_agent.tools}
    assert "fetch_logs" in names
    assert "get_service_metadata" in names


def test_log_analyst_agent_no_extra_tools(log_analyst_agent: Agent[LogAnalysis]) -> None:
    """Log analyst must not have deploy/remediation tools — scope is narrow."""
    names = {t.name for t in log_analyst_agent.tools}
    unexpected = {
        "list_recent_deploys",
        "search_past_incidents",
        "draft_rollback_pr",
        "request_human_approval",
    }
    assert not names & unexpected, f"Unexpected tools: {names & unexpected}"


def test_log_analyst_agent_output_type(log_analyst_agent: Agent[LogAnalysis]) -> None:
    assert log_analyst_agent.output_type is None


def test_log_analyst_agent_has_instructions(log_analyst_agent: Agent[LogAnalysis]) -> None:
    instructions = log_analyst_agent.instructions
    assert isinstance(instructions, str) and len(instructions) > 0


def test_log_analyst_agent_instructions_mention_fetch_logs(
    log_analyst_agent: Agent[LogAnalysis],
) -> None:
    assert isinstance(log_analyst_agent.instructions, str)
    assert "fetch_logs" in log_analyst_agent.instructions


def test_log_analyst_agent_instructions_mention_hypothesis(
    log_analyst_agent: Agent[LogAnalysis],
) -> None:
    assert isinstance(log_analyst_agent.instructions, str)
    assert "hypothesis" in log_analyst_agent.instructions.lower()


def test_log_analyst_agent_uses_litellm_model(log_analyst_agent: Agent[LogAnalysis]) -> None:
    assert isinstance(log_analyst_agent.model, LitellmModel)


def test_log_analyst_agent_model_string_forwarded(
    semantic_memory: SemanticMemory,
    scenario: Scenario,
    fake_settings: Settings,
) -> None:
    agent = build_log_analyst_agent(
        semantic_memory,
        scenario,
        model_string="groq/llama-3.3-70b-versatile",
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "groq/llama-3.3-70b-versatile"


def test_log_analyst_agent_custom_model_string(
    semantic_memory: SemanticMemory,
    scenario: Scenario,
    fake_settings: Settings,
) -> None:
    agent = build_log_analyst_agent(
        semantic_memory,
        scenario,
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "groq/llama-3.1-8b-instant"


def test_log_analyst_agent_no_handoffs(log_analyst_agent: Agent[LogAnalysis]) -> None:
    """Log analyst is a specialist — no handoffs, orchestrator routes."""
    assert len(log_analyst_agent.handoffs) == 0


def test_log_analyst_agent_accepts_none_scenario(
    semantic_memory: SemanticMemory,
    fake_settings: Settings,
) -> None:
    """Agent constructs with no scenario; fetch_logs returns error at call time."""
    agent = build_log_analyst_agent(
        semantic_memory,
        None,
        model_string="groq/llama-3.3-70b-versatile",
        settings=fake_settings,
    )
    assert isinstance(agent, Agent)
    assert len(agent.tools) == 2


def test_log_analyst_agent_mock_memory(
    scenario: Scenario,
    fake_settings: Settings,
) -> None:
    """Agent should construct with a mocked SemanticMemory."""
    mock_mem = MagicMock(spec=SemanticMemory)
    agent = build_log_analyst_agent(
        mock_mem,
        scenario,
        model_string="groq/llama-3.3-70b-versatile",
        settings=fake_settings,
    )
    assert isinstance(agent, Agent)
