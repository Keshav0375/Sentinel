"""Tests for sentinel.agents.remediation — RemediationPlan model and agent structure.

Critical safety invariant verified here: the remediation agent holds exactly the two
output-only drafting tools. HITL is the revert PR a human merges on GitHub
(backend.md §3.3), so the agent has no approval tool and nothing that acts on the world.
"""

from __future__ import annotations

import pytest
from agents import Agent
from agents.extensions.models.litellm_model import LitellmModel

from sentinel.agents.remediation import REMEDIATION_AGENT_NAME, build_remediation_agent
from sentinel.config import Settings
from sentinel.models.remediation import RemediationAction, RemediationPlan

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def remediation_agent(fake_settings: Settings) -> Agent[RemediationPlan]:
    return build_remediation_agent(
        settings=fake_settings,
    )


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
        RemediationPlan.model_validate({"action_type": "rollback", "justification": "x"})


def test_remediation_plan_requires_justification() -> None:
    with pytest.raises(Exception):
        RemediationPlan.model_validate({"action_type": "rollback", "risk_assessment": "low risk"})


def test_remediation_plan_requires_action_type() -> None:
    with pytest.raises(Exception):
        RemediationPlan.model_validate({"risk_assessment": "low", "justification": "something"})


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


# ── build_remediation_agent — structural tests ────────────────────────────────


def test_build_remediation_agent_returns_agent(
    remediation_agent: Agent[RemediationPlan],
) -> None:
    assert isinstance(remediation_agent, Agent)


def test_remediation_agent_name(remediation_agent: Agent[RemediationPlan]) -> None:
    assert remediation_agent.name == REMEDIATION_AGENT_NAME


def test_remediation_agent_name_constant() -> None:
    assert REMEDIATION_AGENT_NAME == "remediation_agent"


def test_remediation_agent_tool_names(remediation_agent: Agent[RemediationPlan]) -> None:
    """CRITICAL: exactly the two output-only drafting tools — nothing that acts or approves."""
    names = [t.name for t in remediation_agent.tools]
    assert names == ["prepare_rollback_spec", "format_escalation"]


# ── CRITICAL SAFETY TEST ──────────────────────────────────────────────────────


def test_remediation_agent_no_direct_execution_tools(
    remediation_agent: Agent[RemediationPlan],
) -> None:
    """Agent must only have draft tools — no direct-execute tools."""
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
    assert remediation_agent.output_type is None


def test_remediation_agent_has_instructions(remediation_agent: Agent[RemediationPlan]) -> None:
    instructions = remediation_agent.instructions
    assert isinstance(instructions, str) and len(instructions) > 0


def test_remediation_agent_instructions_say_a_human_merges_the_pr(
    remediation_agent: Agent[RemediationPlan],
) -> None:
    """The prompt says the backend only drafts and a human merges the revert PR on GitHub."""
    assert isinstance(remediation_agent.instructions, str)
    prompt = remediation_agent.instructions.lower()
    assert "only draft" in prompt
    assert "human merges" in prompt
    assert "github" in prompt
    assert "approval" not in prompt


def test_remediation_agent_instructions_name_the_drafting_tools(
    remediation_agent: Agent[RemediationPlan],
) -> None:
    assert isinstance(remediation_agent.instructions, str)
    assert "prepare_rollback_spec" in remediation_agent.instructions
    assert "format_escalation" in remediation_agent.instructions


def test_remediation_agent_uses_litellm_model(remediation_agent: Agent[RemediationPlan]) -> None:
    assert isinstance(remediation_agent.model, LitellmModel)


def test_remediation_agent_runs_on_its_role_primary_model(fake_settings: Settings) -> None:
    agent = build_remediation_agent(
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "anthropic/claude-sonnet-4-6"


def test_remediation_agent_primary_openai_runs_openai_model(fake_settings: Settings) -> None:
    agent = build_remediation_agent(
        settings=fake_settings.model_copy(update={"sentinel_primary_provider": "openai"}),
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "openai/gpt-4o"


def test_remediation_agent_no_handoffs(remediation_agent: Agent[RemediationPlan]) -> None:
    assert len(remediation_agent.handoffs) == 0
