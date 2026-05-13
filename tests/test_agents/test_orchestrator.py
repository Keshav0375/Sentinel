"""Tests for sentinel.agents.orchestrator — IncidentSummary model and agent structure."""

from __future__ import annotations

from typing import Any

import pytest
from agents import Agent
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from sentinel.agents.comms import COMMS_AGENT_NAME
from sentinel.agents.deploy_correlator import DEPLOY_CORRELATOR_AGENT_NAME
from sentinel.agents.log_analyst import LOG_ANALYST_AGENT_NAME
from sentinel.agents.orchestrator import (
    ORCHESTRATOR_AGENT_NAME,
    SPECIALIST_ORDER,
    build_orchestrator_agent,
)
from sentinel.agents.remediation import REMEDIATION_AGENT_NAME
from sentinel.agents.triage import TRIAGE_AGENT_NAME
from sentinel.models.incident import IncidentSummary, Severity

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def groq_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key="test-key", base_url="https://api.groq.com/openai/v1")


def _dummy_agent(name: str, groq_client: AsyncOpenAI) -> Agent[Any]:
    """Minimal Agent stub — no tools, no handoffs — used to verify wiring."""
    llm = OpenAIChatCompletionsModel(model="llama-3.1-8b-instant", openai_client=groq_client)
    return Agent(name=name, instructions="dummy specialist", model=llm)


@pytest.fixture()
def specialists(groq_client: AsyncOpenAI) -> dict[str, Agent[Any]]:
    return {
        "triage": _dummy_agent(TRIAGE_AGENT_NAME, groq_client),
        "log_analyst": _dummy_agent(LOG_ANALYST_AGENT_NAME, groq_client),
        "deploy_correlator": _dummy_agent(DEPLOY_CORRELATOR_AGENT_NAME, groq_client),
        "remediation": _dummy_agent(REMEDIATION_AGENT_NAME, groq_client),
        "comms": _dummy_agent(COMMS_AGENT_NAME, groq_client),
    }


@pytest.fixture()
def orchestrator(
    specialists: dict[str, Agent[Any]],
    groq_client: AsyncOpenAI,
) -> Agent[IncidentSummary]:
    return build_orchestrator_agent(
        triage_agent=specialists["triage"],
        log_analyst_agent=specialists["log_analyst"],
        deploy_correlator_agent=specialists["deploy_correlator"],
        remediation_agent=specialists["remediation"],
        comms_agent=specialists["comms"],
        groq_client=groq_client,
    )


# ── IncidentSummary model ─────────────────────────────────────────────────────


def test_incident_summary_resolved() -> None:
    summary = IncidentSummary(
        status="resolved",
        affected_service="api-gateway",
        severity=Severity.P1,
        root_cause_summary="Null pointer in AuthMiddleware from deploy-abc123.",
        action_taken="Rolled back deploy-abc123 after human approval.",
    )
    assert summary.status == "resolved"
    assert summary.severity == Severity.P1
    assert summary.next_steps == []


def test_incident_summary_pending_approval() -> None:
    summary = IncidentSummary(
        status="pending_approval",
        affected_service="payment-service",
        severity=Severity.P2,
        root_cause_summary="Connection pool exhaustion — no recent deploy culprit.",
        action_taken="Hotfix drafted, awaiting engineer approval.",
        next_steps=["Approve hotfix in HITL gate", "Monitor DB connection count"],
    )
    assert summary.status == "pending_approval"
    assert len(summary.next_steps) == 2


def test_incident_summary_escalated() -> None:
    summary = IncidentSummary(
        status="escalated",
        affected_service="auth-service",
        severity=Severity.P1,
        root_cause_summary="Unable to determine root cause — insufficient log coverage.",
        action_taken="none",
        next_steps=["Manual investigation required", "Check downstream auth-service logs"],
    )
    assert summary.status == "escalated"
    assert summary.action_taken == "none"


def test_incident_summary_all_terminal_states() -> None:
    for status in ("resolved", "pending_approval", "escalated"):
        summary = IncidentSummary(
            status=status,  # type: ignore[arg-type]
            affected_service="user-service",
            severity=Severity.P3,
            root_cause_summary="some cause",
            action_taken="some action",
        )
        assert summary.status == status


def test_incident_summary_requires_status() -> None:
    with pytest.raises(Exception):
        IncidentSummary.model_validate(
            {
                "affected_service": "api-gateway",
                "severity": "P1",
                "root_cause_summary": "x",
                "action_taken": "y",
            }
        )


def test_incident_summary_requires_severity() -> None:
    with pytest.raises(Exception):
        IncidentSummary.model_validate(
            {
                "status": "resolved",
                "affected_service": "api-gateway",
                "root_cause_summary": "x",
                "action_taken": "y",
            }
        )


def test_incident_summary_rejects_invalid_status() -> None:
    with pytest.raises(Exception):
        IncidentSummary.model_validate(
            {
                "status": "in_progress",  # not a valid Literal
                "affected_service": "api-gateway",
                "severity": "P1",
                "root_cause_summary": "x",
                "action_taken": "y",
            }
        )


def test_incident_summary_json_roundtrip() -> None:
    original = IncidentSummary(
        status="resolved",
        affected_service="order-service",
        severity=Severity.P2,
        root_cause_summary="Missing env var PAYMENT_URL in new deploy.",
        action_taken="Rollback of deploy-env99 approved by keshav.",
        next_steps=["Post-mortem in 48h", "Add env var to deployment checklist"],
    )
    restored = IncidentSummary.model_validate_json(original.model_dump_json())
    assert restored == original


def test_incident_summary_serialization() -> None:
    summary = IncidentSummary(
        status="escalated",
        affected_service="analytics-pipeline",
        severity=Severity.P4,
        root_cause_summary="Low-priority metric anomaly.",
        action_taken="none",
    )
    data = summary.model_dump()
    assert data["status"] == "escalated"
    assert data["severity"] == "P4"
    assert data["next_steps"] == []


# ── build_orchestrator_agent — structural tests ───────────────────────────────


def test_build_orchestrator_agent_returns_agent(
    orchestrator: Agent[IncidentSummary],
) -> None:
    assert isinstance(orchestrator, Agent)


def test_orchestrator_agent_name(orchestrator: Agent[IncidentSummary]) -> None:
    assert orchestrator.name == ORCHESTRATOR_AGENT_NAME


def test_orchestrator_agent_name_constant() -> None:
    assert ORCHESTRATOR_AGENT_NAME == "incident_orchestrator"


def test_orchestrator_has_five_handoffs(orchestrator: Agent[IncidentSummary]) -> None:
    """Orchestrator must wire all five specialist agents."""
    assert len(orchestrator.handoffs) == 5


def test_orchestrator_handoff_names_match_specialists(
    orchestrator: Agent[IncidentSummary],
) -> None:
    """Each handoff must correspond to the correct specialist agent name."""
    names = {h.name for h in orchestrator.handoffs}
    assert TRIAGE_AGENT_NAME in names
    assert LOG_ANALYST_AGENT_NAME in names
    assert DEPLOY_CORRELATOR_AGENT_NAME in names
    assert REMEDIATION_AGENT_NAME in names
    assert COMMS_AGENT_NAME in names


def test_orchestrator_handoff_order(orchestrator: Agent[IncidentSummary]) -> None:
    """Handoffs must be wired in the documented workflow order."""
    names = [h.name for h in orchestrator.handoffs]
    assert names == list(SPECIALIST_ORDER)


def test_specialist_order_constant() -> None:
    """SPECIALIST_ORDER must name all five agents in the correct pipeline order."""
    assert SPECIALIST_ORDER == (
        "triage_agent",
        "log_analyst_agent",
        "deploy_correlator_agent",
        "remediation_agent",
        "comms_agent",
    )


def test_orchestrator_has_no_tools(orchestrator: Agent[IncidentSummary]) -> None:
    """Orchestrator routes via handoffs only — it has no direct tools."""
    assert len(orchestrator.tools) == 0


def test_orchestrator_output_type(orchestrator: Agent[IncidentSummary]) -> None:
    assert orchestrator.output_type is not None


def test_orchestrator_has_instructions(orchestrator: Agent[IncidentSummary]) -> None:
    instructions = orchestrator.instructions
    assert isinstance(instructions, str) and len(instructions) > 0


def test_orchestrator_instructions_mention_tool_call_cap(
    orchestrator: Agent[IncidentSummary],
) -> None:
    assert isinstance(orchestrator.instructions, str)
    assert "15" in orchestrator.instructions


def test_orchestrator_instructions_mention_all_specialists(
    orchestrator: Agent[IncidentSummary],
) -> None:
    assert isinstance(orchestrator.instructions, str)
    for specialist in ("Triage", "Log Analyst", "Deploy Correlator", "Remediation", "Comms"):
        assert specialist in orchestrator.instructions, (
            f"orchestrator.txt must mention {specialist}"
        )


def test_orchestrator_instructions_mention_escalation(
    orchestrator: Agent[IncidentSummary],
) -> None:
    assert isinstance(orchestrator.instructions, str)
    assert "escalat" in orchestrator.instructions.lower()


def test_orchestrator_uses_groq_model(orchestrator: Agent[IncidentSummary]) -> None:
    assert isinstance(orchestrator.model, OpenAIChatCompletionsModel)


def test_orchestrator_default_model_name(
    specialists: dict[str, Agent[Any]],
    groq_client: AsyncOpenAI,
) -> None:
    agent = build_orchestrator_agent(
        triage_agent=specialists["triage"],
        log_analyst_agent=specialists["log_analyst"],
        deploy_correlator_agent=specialists["deploy_correlator"],
        remediation_agent=specialists["remediation"],
        comms_agent=specialists["comms"],
        groq_client=groq_client,
    )
    assert isinstance(agent.model, OpenAIChatCompletionsModel)
    assert agent.model.model == "llama-3.3-70b-versatile"


def test_orchestrator_custom_model_name(
    specialists: dict[str, Agent[Any]],
    groq_client: AsyncOpenAI,
) -> None:
    agent = build_orchestrator_agent(
        triage_agent=specialists["triage"],
        log_analyst_agent=specialists["log_analyst"],
        deploy_correlator_agent=specialists["deploy_correlator"],
        remediation_agent=specialists["remediation"],
        comms_agent=specialists["comms"],
        groq_client=groq_client,
        model_name="llama-3.1-8b-instant",
    )
    assert isinstance(agent.model, OpenAIChatCompletionsModel)
    assert agent.model.model == "llama-3.1-8b-instant"


def test_orchestrator_itself_has_no_handoffs_on_specialists(
    orchestrator: Agent[IncidentSummary],
) -> None:
    """Each specialist in the handoffs list should have no sub-handoffs."""
    for specialist in orchestrator.handoffs:
        assert len(specialist.handoffs) == 0, (
            f"{specialist.name} should not have handoffs — only orchestrator routes"
        )
