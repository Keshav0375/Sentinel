"""Tests for sentinel.agents.comms — SlackSummary model and agent structure."""

from __future__ import annotations

import pytest
from agents import Agent
from agents.extensions.models.litellm_model import LitellmModel

from sentinel.agents.comms import COMMS_AGENT_NAME, build_comms_agent
from sentinel.config import Settings
from sentinel.models.comms import SlackSummary

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def comms_agent(fake_settings: Settings) -> Agent[SlackSummary]:
    return build_comms_agent(
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )


# ── SlackSummary model ────────────────────────────────────────────────────────


def test_slack_summary_basic_construction() -> None:
    summary = SlackSummary(
        slack_message=":rotating_light: *Incident Summary* — api-gateway (P1)\n\n*Impact*\n...",
        recipients=["#incidents", "#oncall-platform"],
    )
    assert "#incidents" in summary.recipients
    assert "#oncall-platform" in summary.recipients
    assert "api-gateway" in summary.slack_message


def test_slack_summary_default_recipient() -> None:
    """Default recipients must always include #incidents."""
    summary = SlackSummary(slack_message="some message")
    assert "#incidents" in summary.recipients


def test_slack_summary_p1_oncall_channel() -> None:
    """P1/P2 summaries should include the team oncall channel."""
    summary = SlackSummary(
        slack_message="P1 incident",
        recipients=["#incidents", "#oncall-identity"],
    )
    assert len(summary.recipients) == 2
    oncall = [r for r in summary.recipients if r.startswith("#oncall-")]
    assert len(oncall) == 1


def test_slack_summary_p3_incidents_only() -> None:
    """P3/P4 summaries go to #incidents only."""
    summary = SlackSummary(
        slack_message="P3 incident",
        recipients=["#incidents"],
    )
    assert summary.recipients == ["#incidents"]


def test_slack_summary_requires_slack_message() -> None:
    with pytest.raises(Exception):
        SlackSummary.model_validate({"recipients": ["#incidents"]})


def test_slack_summary_json_roundtrip() -> None:
    original = SlackSummary(
        slack_message="*Impact*\nService api-gateway degraded.\n\n*Root Cause*\nBad deploy.",
        recipients=["#incidents", "#oncall-platform"],
    )
    restored = SlackSummary.model_validate_json(original.model_dump_json())
    assert restored == original


def test_slack_summary_serialization() -> None:
    summary = SlackSummary(
        slack_message="test message",
        recipients=["#incidents"],
    )
    data = summary.model_dump()
    assert data["slack_message"] == "test message"
    assert data["recipients"] == ["#incidents"]


def test_slack_summary_multiple_recipients() -> None:
    summary = SlackSummary(
        slack_message="critical incident",
        recipients=["#incidents", "#oncall-payments", "#sre-escalation"],
    )
    assert len(summary.recipients) == 3


def test_slack_summary_message_contains_sections() -> None:
    """Verify a well-formed Slack summary has all required sections."""
    message = (
        ":rotating_light: *Incident Summary* — payment-service (P1)\n\n"
        "*Impact*\n100% error rate on /checkout\n\n"
        "*Root Cause*\nNull pointer in PaymentProcessor\n\n"
        "*Timeline*\n14:00 alert → 14:05 triage → 14:12 rollback approved\n\n"
        "*Current Status*\nResolved\n\n"
        "*Action Items*\nMonitor error rate for 10 minutes\n\n"
        "*ETA to Resolution*\nResolved"
    )
    summary = SlackSummary(slack_message=message, recipients=["#incidents"])

    for section in ["Impact", "Root Cause", "Timeline", "Current Status", "Action Items", "ETA"]:
        assert section in summary.slack_message, f"Missing section: {section}"


# ── build_comms_agent — structural tests ─────────────────────────────────────


def test_build_comms_agent_returns_agent(comms_agent: Agent[SlackSummary]) -> None:
    assert isinstance(comms_agent, Agent)


def test_comms_agent_name(comms_agent: Agent[SlackSummary]) -> None:
    assert comms_agent.name == COMMS_AGENT_NAME


def test_comms_agent_name_constant() -> None:
    assert COMMS_AGENT_NAME == "comms_agent"


def test_comms_agent_has_one_tool(comms_agent: Agent[SlackSummary]) -> None:
    """Comms agent has exactly one tool — draft_slack_summary."""
    assert len(comms_agent.tools) == 1


def test_comms_agent_tool_name(comms_agent: Agent[SlackSummary]) -> None:
    assert comms_agent.tools[0].name == "draft_slack_summary"


def test_comms_agent_no_extra_tools(comms_agent: Agent[SlackSummary]) -> None:
    names = {t.name for t in comms_agent.tools}
    unexpected = {
        "fetch_logs",
        "get_service_metadata",
        "list_recent_deploys",
        "search_past_incidents",
        "draft_rollback_pr",
        "request_human_approval",
    }
    assert not names & unexpected, f"Unexpected tools: {names & unexpected}"


def test_comms_agent_output_type(comms_agent: Agent[SlackSummary]) -> None:
    # Groq does not support native structured outputs via LiteLLM, so output_type is None
    assert comms_agent.output_type is None


def test_comms_agent_has_instructions(comms_agent: Agent[SlackSummary]) -> None:
    instructions = comms_agent.instructions
    assert isinstance(instructions, str) and len(instructions) > 0


def test_comms_agent_instructions_mention_draft_slack_summary(
    comms_agent: Agent[SlackSummary],
) -> None:
    assert isinstance(comms_agent.instructions, str)
    assert "draft_slack_summary" in comms_agent.instructions


def test_comms_agent_instructions_mention_required_sections(
    comms_agent: Agent[SlackSummary],
) -> None:
    """System prompt must list all six mandatory Slack message sections."""
    assert isinstance(comms_agent.instructions, str)
    for section in ["Impact", "Root Cause", "Timeline", "Current Status", "Action Items", "ETA"]:
        assert section in comms_agent.instructions, (
            f"comms.txt must mention required section '{section}'"
        )


def test_comms_agent_instructions_mention_incidents_channel(
    comms_agent: Agent[SlackSummary],
) -> None:
    assert isinstance(comms_agent.instructions, str)
    assert "#incidents" in comms_agent.instructions


def test_comms_agent_uses_litellm_model(comms_agent: Agent[SlackSummary]) -> None:
    assert isinstance(comms_agent.model, LitellmModel)


def test_comms_agent_model_string_forwarded(fake_settings: Settings) -> None:
    agent = build_comms_agent(
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "groq/llama-3.1-8b-instant"


def test_comms_agent_custom_model_string(fake_settings: Settings) -> None:
    agent = build_comms_agent(
        model_string="groq/llama-3.3-70b-versatile",
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "groq/llama-3.3-70b-versatile"


def test_comms_agent_no_handoffs(comms_agent: Agent[SlackSummary]) -> None:
    """Comms agent is a specialist — no handoffs, orchestrator routes."""
    assert len(comms_agent.handoffs) == 0
