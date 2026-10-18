"""Tests for sentinel.agents.resolution — the drafting agent (renamed remediation).

Safety invariant verified here: the agent holds exactly the two output-only drafting tools.
HITL is the revert PR a human merges on GitHub (backend.md §3.3), so the agent has no
approval tool and nothing that acts on the world.
"""

from __future__ import annotations

import json

import pytest
from agents import Agent, Runner
from agents.extensions.models.litellm_model import LitellmModel

from sentinel.agents.loader import load_prompt
from sentinel.agents.resolution import RESOLUTION_AGENT_NAME, build_resolution_agent
from sentinel.config import Settings
from sentinel.models.tools import RollbackSpec
from sentinel.providers import FakeToolCall, FakeTurn, fake_script


@pytest.fixture()
def resolution_agent(fake_settings: Settings) -> Agent[object]:
    return build_resolution_agent(settings=fake_settings)


def test_agent_name_is_the_prompt_name(resolution_agent: Agent[object]) -> None:
    assert RESOLUTION_AGENT_NAME == "resolution"
    assert resolution_agent.name == "resolution"


def test_agent_holds_only_the_two_drafting_tools(resolution_agent: Agent[object]) -> None:
    assert sorted(t.name for t in resolution_agent.tools) == [
        "format_escalation",
        "prepare_rollback_spec",
    ]
    assert resolution_agent.handoffs == []


def test_agent_runs_on_the_resolution_role_models(fake_settings: Settings) -> None:
    agent = build_resolution_agent(settings=fake_settings)
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "anthropic/claude-sonnet-4-6"
    openai = fake_settings.model_copy(update={"sentinel_primary_provider": "openai"})
    agent = build_resolution_agent(settings=openai)
    assert isinstance(agent.model, LitellmModel) and agent.model.model == "openai/gpt-4o"


def test_prompt_keeps_the_human_gate() -> None:
    lower = load_prompt("resolution.txt").lower()
    assert "non-negotiable" in lower and "only drafts" in lower
    assert "a human merges it" in lower and "github" in lower
    assert "approval" not in lower
    assert "prepare_rollback_spec" in lower and "format_escalation" in lower
    assert "0.7" in lower


async def test_agent_drafts_a_rollback_spec_through_the_tool() -> None:
    settings = Settings(_env_file=None, sentinel_fake_llm=True)  # type: ignore[call-arg]
    agent = build_resolution_agent(settings=settings)
    request = {
        "incident_id": "inc-1",
        "root_cause": "pr-42 broke GET /",
        "evidence_summary": "every check failed after the deploy",
        "confidence": 0.9,
        "justification": "the previous version passed",
        "target_deploy": {
            "pr_number": 42,
            "commit_sha": "A1B2C3D",
            "deployed_at": "2026-10-15T12:00:00Z",
        },
    }
    with fake_script() as script:
        script.on(
            RESOLUTION_AGENT_NAME,
            FakeTurn(tool_calls=[FakeToolCall("prepare_rollback_spec", {"request": request})]),
            FakeTurn(output="Drafted the rollback of PR #42."),
        )
        result = await Runner.run(agent, "findings")

    outputs = [item.output for item in result.new_items if hasattr(item, "output")]
    spec = RollbackSpec.model_validate(json.loads(str(outputs[0])))
    assert spec.target_deploy.commit_sha == "a1b2c3d"
    assert spec.resolution_type == "rollback"
