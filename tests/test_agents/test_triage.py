"""Tests for sentinel.agents.triage — TriageResult model and agent structure."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agents import Agent
from agents.extensions.models.litellm_model import LitellmModel

from data.seed import seed
from sentinel.agents.triage import TRIAGE_AGENT_NAME, build_triage_agent
from sentinel.config import Settings
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.incident import Severity, TriageResult

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test_triage.db"
    await seed(path, verbose=False)
    return path


@pytest.fixture()
def semantic_memory(db_path: Path) -> SemanticMemory:
    return SemanticMemory(db_path)


@pytest.fixture()
def episodic_memory(db_path: Path) -> EpisodicMemory:
    mock_client = MagicMock()
    return EpisodicMemory(db_path, mock_client)


@pytest.fixture()
def triage_agent(
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    fake_settings: Settings,
) -> Agent[TriageResult]:
    return build_triage_agent(
        semantic_memory,
        episodic_memory,
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )


# ── TriageResult model ────────────────────────────────────────────────────────


def test_triage_result_valid_construction() -> None:
    result = TriageResult(
        severity=Severity.P1,
        affected_service="api-gateway",
        is_duplicate=False,
        recommended_action="Analyze logs for api-gateway from 03:00-03:15 UTC",
    )
    assert result.severity == Severity.P1
    assert result.affected_service == "api-gateway"
    assert result.is_duplicate is False
    assert "api-gateway" in result.recommended_action


def test_triage_result_all_severities() -> None:
    for sev in Severity:
        result = TriageResult(
            severity=sev,
            affected_service="payment-service",
            is_duplicate=False,
            recommended_action="investigate",
        )
        assert result.severity == sev


def test_triage_result_duplicate_flag() -> None:
    result = TriageResult(
        severity=Severity.P2,
        affected_service="user-service",
        is_duplicate=True,
        recommended_action="Duplicate of inc-abc123, no further action needed",
    )
    assert result.is_duplicate is True


def test_triage_result_serialization() -> None:
    result = TriageResult(
        severity=Severity.P2,
        affected_service="auth-service",
        is_duplicate=False,
        recommended_action="Check recent deploys",
    )
    data = result.model_dump()
    assert data["severity"] == "P2"
    assert data["affected_service"] == "auth-service"
    assert data["is_duplicate"] is False


def test_triage_result_json_roundtrip() -> None:
    original = TriageResult(
        severity=Severity.P3,
        affected_service="notification-service",
        is_duplicate=False,
        recommended_action="Monitor for escalation",
    )
    restored = TriageResult.model_validate_json(original.model_dump_json())
    assert restored == original


def test_triage_result_requires_severity() -> None:
    with pytest.raises(Exception):
        TriageResult.model_validate(
            {"affected_service": "api-gateway", "is_duplicate": False, "recommended_action": "x"}
        )


def test_triage_result_requires_affected_service() -> None:
    with pytest.raises(Exception):
        TriageResult.model_validate(
            {"severity": "P1", "is_duplicate": False, "recommended_action": "x"}
        )


# ── build_triage_agent — structural tests ─────────────────────────────────────


def test_build_triage_agent_returns_agent(triage_agent: Agent[TriageResult]) -> None:
    assert isinstance(triage_agent, Agent)


def test_triage_agent_name(triage_agent: Agent[TriageResult]) -> None:
    assert triage_agent.name == TRIAGE_AGENT_NAME


def test_triage_agent_name_constant() -> None:
    assert TRIAGE_AGENT_NAME == "triage_agent"


def test_triage_agent_has_two_tools(triage_agent: Agent[TriageResult]) -> None:
    assert len(triage_agent.tools) == 2


def test_triage_agent_tool_names(triage_agent: Agent[TriageResult]) -> None:
    names = {t.name for t in triage_agent.tools}
    assert "get_service_metadata" in names
    assert "search_past_incidents" in names


def test_triage_agent_no_extra_tools(triage_agent: Agent[TriageResult]) -> None:
    """Triage agent must not have deploy/log/remediation tools — scope is intentionally narrow."""
    names = {t.name for t in triage_agent.tools}
    unexpected = {
        "fetch_logs",
        "list_recent_deploys",
        "draft_rollback_pr",
        "request_human_approval",
    }
    assert not names & unexpected, f"Unexpected tools in triage agent: {names & unexpected}"


def test_triage_agent_output_type(triage_agent: Agent[TriageResult]) -> None:
    assert triage_agent.output_type is None


def test_triage_agent_has_instructions(triage_agent: Agent[TriageResult]) -> None:
    instructions = triage_agent.instructions
    assert isinstance(instructions, str) and len(instructions) > 0


def test_triage_agent_instructions_mention_severity(triage_agent: Agent[TriageResult]) -> None:
    assert isinstance(triage_agent.instructions, str)
    assert "P1" in triage_agent.instructions


def test_triage_agent_uses_litellm_model(triage_agent: Agent[TriageResult]) -> None:
    assert isinstance(triage_agent.model, LitellmModel)


def test_triage_agent_model_string_forwarded(
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    fake_settings: Settings,
) -> None:
    agent = build_triage_agent(
        semantic_memory,
        episodic_memory,
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "groq/llama-3.1-8b-instant"


def test_triage_agent_custom_model_string(
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    fake_settings: Settings,
) -> None:
    agent = build_triage_agent(
        semantic_memory,
        episodic_memory,
        model_string="groq/llama-3.3-70b-versatile",
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "groq/llama-3.3-70b-versatile"


def test_triage_agent_no_handoffs(triage_agent: Agent[TriageResult]) -> None:
    """Triage agent is a specialist — it has no handoffs (orchestrator routes, not specialists)."""
    assert len(triage_agent.handoffs) == 0
