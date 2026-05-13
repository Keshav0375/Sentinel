"""Tests for sentinel.agents.remediation — RemediationPlan model and agent structure.

Critical safety invariant verified here:
  ``request_human_approval`` MUST be present in the remediation agent's tool list.
  Without it, the agent could theoretically propose destructive actions without
  a human in the loop. This is the structural half of the HITL contract;
  the system-prompt half is enforced in test_prompts.py.
"""

from __future__ import annotations

import pytest
from agents import Agent
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from sentinel.agents.remediation import REMEDIATION_AGENT_NAME, build_remediation_agent
from sentinel.models.remediation import (
    ApprovalStatus,
    RemediationAction,
    RemediationPlan,
    RiskLevel,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def groq_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key="test-key", base_url="https://api.groq.com/openai/v1")


@pytest.fixture()
def remediation_agent(groq_client: AsyncOpenAI) -> Agent[RemediationPlan]:
    return build_remediation_agent(groq_client=groq_client)


# ── RemediationPlan model ─────────────────────────────────────────────────────


def test_remediation_plan_rollback() -> None:
    plan = RemediationPlan(
        action_type=RemediationAction.ROLLBACK,
        deploy_id="deploy-abc123",
        risk_assessment="Rolling back a deploy carries low risk if CI passes.",
        justification="deploy-abc123 introduced a null pointer in AuthMiddleware.",
    )
    assert plan.action_type == RemediationAction.ROLLBACK
    assert plan.deploy_id == "deploy-abc123"
    assert plan.diff is None


def test_remediation_plan_hotfix() -> None:
    plan = RemediationPlan(
        action_type=RemediationAction.HOTFIX,
        diff="--- a/auth.py\n+++ b/auth.py\n@@ -1 +1 @@\n-config['key']\n+config.get('key')",
        risk_assessment="Minimal change — only adds a None guard on config key lookup.",
        justification="No clear culprit deploy; fix the null guard directly.",
    )
    assert plan.action_type == RemediationAction.HOTFIX
    assert plan.diff is not None
    assert plan.deploy_id is None


def test_remediation_plan_escalate() -> None:
    plan = RemediationPlan(
        action_type=RemediationAction.ESCALATE,
        risk_assessment="Insufficient evidence to propose a safe fix.",
        justification="Confidence below 0.3 — manual investigation required.",
    )
    assert plan.action_type == RemediationAction.ESCALATE
    assert plan.deploy_id is None
    assert plan.diff is None


def test_remediation_plan_all_action_types() -> None:
    for action in RemediationAction:
        plan = RemediationPlan(
            action_type=action,
            risk_assessment="assessed",
            justification="justified",
        )
        assert plan.action_type == action


def test_remediation_plan_requires_risk_assessment() -> None:
    with pytest.raises(Exception):
        RemediationPlan.model_validate(
            {"action_type": "rollback", "justification": "x"}
        )


def test_remediation_plan_requires_justification() -> None:
    with pytest.raises(Exception):
        RemediationPlan.model_validate(
            {"action_type": "rollback", "risk_assessment": "low risk"}
        )


def test_remediation_plan_requires_action_type() -> None:
    with pytest.raises(Exception):
        RemediationPlan.model_validate(
            {"risk_assessment": "low", "justification": "something"}
        )


def test_remediation_plan_json_roundtrip() -> None:
    original = RemediationPlan(
        action_type=RemediationAction.ROLLBACK,
        deploy_id="deploy-xyz789",
        risk_assessment="High risk — rolling back a deploy that touched payment flows.",
        justification="deploy-xyz789 is the only deploy in the window; timing matches.",
    )
    restored = RemediationPlan.model_validate_json(original.model_dump_json())
    assert restored == original


def test_remediation_plan_serialization() -> None:
    plan = RemediationPlan(
        action_type=RemediationAction.HOTFIX,
        diff="@@ fix @@",
        risk_assessment="Low risk change.",
        justification="Adding a null guard.",
    )
    data = plan.model_dump()
    assert data["action_type"] == "hotfix"
    assert data["diff"] == "@@ fix @@"
    assert data["deploy_id"] is None


# ── Risk level and approval status enums ─────────────────────────────────────


def test_risk_level_values() -> None:
    assert RiskLevel.HIGH == "high"
    assert RiskLevel.MEDIUM == "medium"
    assert RiskLevel.LOW == "low"


def test_approval_status_values() -> None:
    assert ApprovalStatus.APPROVED == "approved"
    assert ApprovalStatus.REJECTED == "rejected"
    assert ApprovalStatus.TIMEOUT == "timeout"


# ── build_remediation_agent — structural tests ────────────────────────────────


def test_build_remediation_agent_returns_agent(
    remediation_agent: Agent[RemediationPlan],
) -> None:
    assert isinstance(remediation_agent, Agent)


def test_remediation_agent_name(remediation_agent: Agent[RemediationPlan]) -> None:
    assert remediation_agent.name == REMEDIATION_AGENT_NAME


def test_remediation_agent_name_constant() -> None:
    assert REMEDIATION_AGENT_NAME == "remediation_agent"


def test_remediation_agent_has_three_tools(remediation_agent: Agent[RemediationPlan]) -> None:
    assert len(remediation_agent.tools) == 3


def test_remediation_agent_tool_names(remediation_agent: Agent[RemediationPlan]) -> None:
    names = {t.name for t in remediation_agent.tools}
    assert "draft_rollback_pr" in names
    assert "draft_hotfix" in names
    assert "request_human_approval" in names


# ── CRITICAL SAFETY TEST ──────────────────────────────────────────────────────


def test_remediation_agent_hitl_gate_present(remediation_agent: Agent[RemediationPlan]) -> None:
    """CRITICAL: request_human_approval MUST be in the remediation agent's tool list.

    This is the structural half of the HITL safety contract. The agent cannot
    propose a destructive action without calling this tool first. If this test
    fails, the safety boundary has been broken — fix it before merging.
    """
    tool_names = {t.name for t in remediation_agent.tools}
    assert "request_human_approval" in tool_names, (
        "SAFETY VIOLATION: request_human_approval is missing from remediation agent tools. "
        "No destructive action can be proposed without a human in the loop."
    )


def test_remediation_agent_no_direct_execution_tools(
    remediation_agent: Agent[RemediationPlan],
) -> None:
    """Agent must only have draft tools + HITL gate — no direct-execute tools."""
    names = {t.name for t in remediation_agent.tools}
    # These would be dangerous if they existed — verify they don't
    forbidden = {
        "execute_rollback",
        "merge_pr",
        "restart_service",
        "delete_deployment",
    }
    assert not names & forbidden, f"Dangerous direct-execute tools found: {names & forbidden}"


# ── Remaining structural tests ────────────────────────────────────────────────


def test_remediation_agent_output_type(remediation_agent: Agent[RemediationPlan]) -> None:
    assert remediation_agent.output_type is not None


def test_remediation_agent_has_instructions(remediation_agent: Agent[RemediationPlan]) -> None:
    instructions = remediation_agent.instructions
    assert isinstance(instructions, str) and len(instructions) > 0


def test_remediation_agent_instructions_mention_hitl(
    remediation_agent: Agent[RemediationPlan],
) -> None:
    """System prompt must explicitly mandate HITL — structural + prompt contract."""
    assert isinstance(remediation_agent.instructions, str)
    assert "request_human_approval" in remediation_agent.instructions


def test_remediation_agent_instructions_mention_rollback_and_hotfix(
    remediation_agent: Agent[RemediationPlan],
) -> None:
    assert isinstance(remediation_agent.instructions, str)
    assert "rollback" in remediation_agent.instructions.lower()
    assert "hotfix" in remediation_agent.instructions.lower()


def test_remediation_agent_uses_groq_model(remediation_agent: Agent[RemediationPlan]) -> None:
    assert isinstance(remediation_agent.model, OpenAIChatCompletionsModel)


def test_remediation_agent_default_model_name(groq_client: AsyncOpenAI) -> None:
    agent = build_remediation_agent(groq_client=groq_client)
    assert isinstance(agent.model, OpenAIChatCompletionsModel)
    assert agent.model.model == "llama-3.3-70b-versatile"


def test_remediation_agent_custom_model_name(groq_client: AsyncOpenAI) -> None:
    agent = build_remediation_agent(
        groq_client=groq_client, model_name="llama-3.1-8b-instant"
    )
    assert isinstance(agent.model, OpenAIChatCompletionsModel)
    assert agent.model.model == "llama-3.1-8b-instant"


def test_remediation_agent_no_handoffs(remediation_agent: Agent[RemediationPlan]) -> None:
    assert len(remediation_agent.handoffs) == 0


def test_remediation_agent_custom_approval_fn(groq_client: AsyncOpenAI) -> None:
    """Agent must accept an injectable approval_fn for test/Phase 2 use."""
    async def mock_approval(display: str) -> tuple[str, str | None]:
        return "approve", "auto-approved in test"

    agent = build_remediation_agent(groq_client=groq_client, approval_fn=mock_approval)
    assert isinstance(agent, Agent)
    # HITL tool still present even with custom approval function
    names = {t.name for t in agent.tools}
    assert "request_human_approval" in names
