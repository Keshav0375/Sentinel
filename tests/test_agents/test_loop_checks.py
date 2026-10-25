"""Tests for the orchestrator's two loop-check agents: reflexion (§4.3), reverification (§4.4).

Neither has a tool; both run on the fake LLM here, and their replies parse with the 4.3
contracts (ReflexionResult, parse_reverification).
"""

from __future__ import annotations

from agents import Runner
from agents.extensions.models.litellm_model import LitellmModel

from sentinel.agents.reflexion import REFLEXION_AGENT_NAME, build_reflexion_agent
from sentinel.agents.reverification import (
    REVERIFICATION_AGENT_NAME,
    build_reverification_agent,
)
from sentinel.config import Settings
from sentinel.models.reflexion import ReflexionResult, SuspectDeploy
from sentinel.models.reverification import ReverificationOutcome, parse_reverification
from sentinel.providers import coerce_output, fake_script, reflexion_turn, reverification_turn


def _fake_llm() -> Settings:
    return Settings(_env_file=None, sentinel_fake_llm=True)  # type: ignore[call-arg]


def test_names_are_the_prompt_names() -> None:
    assert REFLEXION_AGENT_NAME == "reflexion"
    assert REVERIFICATION_AGENT_NAME == "reverification"


def test_reflexion_agent_has_no_tools_and_runs_on_haiku(fake_settings: Settings) -> None:
    agent = build_reflexion_agent(settings=fake_settings)
    assert agent.tools == [] and agent.handoffs == []
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "anthropic/claude-haiku-4-5"
    assert isinstance(agent.instructions, str)
    assert agent.instructions.startswith("You are the Reflexion Critic")


def test_reverification_agent_has_no_tools_and_plain_text(fake_settings: Settings) -> None:
    agent = build_reverification_agent(settings=fake_settings)
    assert agent.tools == [] and agent.handoffs == []
    assert agent.output_type is None
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "anthropic/claude-haiku-4-5"


async def test_reflexion_reply_coerces_to_the_contract() -> None:
    agent = build_reflexion_agent(settings=_fake_llm())
    deploy = SuspectDeploy(commit_sha="a1b2c3d", pr_number=42)
    with fake_script() as script:
        script.on(REFLEXION_AGENT_NAME, reflexion_turn(0.85, deploy=deploy))
        result = await Runner.run(agent, "hypothesis + evidence")
    critique = coerce_output(str(result.final_output), ReflexionResult)
    assert critique is not None and critique.deploy == deploy
    assert not critique.needs_more_evidence


async def test_reverification_reply_parses_to_its_outcome() -> None:
    agent = build_reverification_agent(settings=_fake_llm())
    with fake_script() as script:
        script.on(REVERIFICATION_AGENT_NAME, reverification_turn("FAIL", "wrong target"))
        result = await Runner.run(agent, "spec")
    verdict = parse_reverification(str(result.final_output))
    assert verdict.outcome is ReverificationOutcome.FAIL
    assert verdict.reason == "wrong target"
