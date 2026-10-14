"""Tests for sentinel.agents.deploy_correlator — DeployCorrelation model and agent structure."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from agents import Agent, AgentOutputSchema
from agents.extensions.models.litellm_model import LitellmModel
from pydantic import ValidationError

from sentinel.agents.deploy_correlator import (
    DEPLOY_CORRELATOR_AGENT_NAME,
    build_deploy_correlator_agent,
)
from sentinel.config import Settings
from sentinel.models.deploy import CorrelatedDeploy, DeployCorrelation
from sentinel.models.tools import (
    DeployRecord,
    RollbackSpec,
    RollbackSpecInput,
    TargetDeploy,
)
from sentinel.tools.remediation_tools import _prepare_rollback_spec

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def pool() -> MagicMock:
    """A stand-in pool: building the agent never touches it."""
    return MagicMock()


@pytest.fixture()
def correlator_agent(
    pool: MagicMock,
    fake_settings: Settings,
) -> Agent[DeployCorrelation]:
    return build_deploy_correlator_agent(
        pool,
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )


def _record(
    pr_number: int | None, sha: str, deployed_at: datetime, status: str = "succeeded"
) -> DeployRecord:
    """A get_deploy_details record as the tool returns it."""
    return DeployRecord(
        pr_number=pr_number,
        commit_sha=sha,
        author="dev-bot",
        deploy_status=status,
        deployed_at=deployed_at,
        files_changed=["app/main.py"],
        failed_stage="verify" if status == "failed" else "none",
        version=f"pr-{pr_number}-{sha}" if pr_number else None,
        gha_run_id=987,
    )


@pytest.fixture()
def sample_deploy() -> CorrelatedDeploy:
    record = _record(42, "a1b2c3d", datetime(2026, 5, 10, 2, 45, 0, tzinfo=UTC), "failed")
    return CorrelatedDeploy.model_validate(record.model_dump())


@pytest.fixture()
def innocent_deploy() -> CorrelatedDeploy:
    record = _record(41, "f7e8d9a", datetime(2026, 5, 9, 14, 0, 0, tzinfo=UTC))
    return CorrelatedDeploy.model_validate(record.model_dump())


# ── DeployCorrelation model ───────────────────────────────────────────────────


def test_deploy_correlation_no_suspect(sample_deploy: CorrelatedDeploy) -> None:
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


def test_deploy_correlation_with_suspect(sample_deploy: CorrelatedDeploy) -> None:
    corr = DeployCorrelation(
        recent_deploys=[sample_deploy],
        suspect_deploy=sample_deploy,
        confidence=0.85,
        evidence="pr-42 changed app/main.py 15 minutes before the first failed check.",
    )
    assert corr.suspect_deploy is not None
    assert corr.suspect_deploy.commit_sha == "a1b2c3d"
    assert corr.suspect_deploy.pr_number == 42
    assert corr.suspect_deploy.version == "pr-42-a1b2c3d"
    assert corr.suspect_deploy.deploy_status == "failed"
    assert corr.confidence == 0.85


def test_deploy_correlation_multiple_deploys(
    sample_deploy: CorrelatedDeploy, innocent_deploy: CorrelatedDeploy
) -> None:
    corr = DeployCorrelation(
        recent_deploys=[sample_deploy, innocent_deploy],
        suspect_deploy=sample_deploy,
        confidence=0.9,
        evidence="pr-42 closest in time and touches the failing handler.",
    )
    assert len(corr.recent_deploys) == 2
    assert corr.suspect_deploy == sample_deploy


def test_deploy_correlation_default_confidence() -> None:
    corr = DeployCorrelation(evidence="No deploys found.")
    assert corr.confidence == 0.0
    assert corr.recent_deploys == []
    assert corr.suspect_deploy is None


def test_deploy_correlation_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        DeployCorrelation.model_validate({"confidence": 0.5})


def test_deploy_correlation_json_roundtrip(sample_deploy: CorrelatedDeploy) -> None:
    original = DeployCorrelation(
        recent_deploys=[sample_deploy],
        suspect_deploy=sample_deploy,
        confidence=0.75,
        evidence="Timing and file scope match the failed checks.",
    )
    restored = DeployCorrelation.model_validate_json(original.model_dump_json())
    assert restored == original


def test_deploy_correlation_serialization(sample_deploy: CorrelatedDeploy) -> None:
    corr = DeployCorrelation(
        recent_deploys=[sample_deploy],
        suspect_deploy=sample_deploy,
        confidence=0.8,
        evidence="pr-42 is the suspect.",
    )
    data = corr.model_dump(mode="json")
    assert data["confidence"] == 0.8
    assert data["suspect_deploy"] == {
        "pr_number": 42,
        "commit_sha": "a1b2c3d",
        "deployed_at": "2026-05-10T02:45:00Z",
        "version": "pr-42-a1b2c3d",
        "deploy_status": "failed",
        "failed_stage": "verify",
    }


def test_deploy_correlation_confidence_range_zero() -> None:
    corr = DeployCorrelation(confidence=0.0, evidence="No correlation.")
    assert corr.confidence == 0.0


def test_deploy_correlation_confidence_range_one(sample_deploy: CorrelatedDeploy) -> None:
    corr = DeployCorrelation(
        recent_deploys=[sample_deploy],
        suspect_deploy=sample_deploy,
        confidence=1.0,
        evidence="Certain culprit — module and timing match exactly.",
    )
    assert corr.confidence == 1.0


# ── CorrelatedDeploy ↔ DeployRecord ↔ prepare_rollback_spec ──────────────────


def test_correlated_deploy_fields_are_a_subset_of_deploy_record() -> None:
    """Same names and annotations as DeployRecord: a record validates in unchanged."""
    for name, field in CorrelatedDeploy.model_fields.items():
        assert name in DeployRecord.model_fields, name
        assert field.annotation == DeployRecord.model_fields[name].annotation, name


def test_correlated_deploy_has_no_default_time() -> None:
    """deployed_at must come from the record — never a default "now"."""
    assert CorrelatedDeploy.model_fields["deployed_at"].is_required()
    assert CorrelatedDeploy.model_fields["commit_sha"].is_required()
    with pytest.raises(ValidationError):
        CorrelatedDeploy.model_validate(
            {"pr_number": 42, "commit_sha": "a1b2c3d", "deploy_status": "failed"}
        )


def test_suspect_deploy_feeds_prepare_rollback_spec_with_the_real_values() -> None:
    """Tool record → correlator output → target_deploy: every value survives verbatim."""
    deployed_at = datetime(2026, 5, 10, 2, 45, 7, tzinfo=UTC)
    record = _record(42, "a1b2c3d4e5f", deployed_at, "failed")
    # The correlator's output as the model would emit it (JSON), parsed back.
    output = DeployCorrelation(
        recent_deploys=[CorrelatedDeploy.model_validate(record.model_dump())],
        suspect_deploy=CorrelatedDeploy.model_validate(record.model_dump()),
        confidence=0.9,
        evidence="pr-42 failed verify minutes before the first failed check.",
    )
    suspect = DeployCorrelation.model_validate_json(output.model_dump_json()).suspect_deploy
    assert suspect is not None

    spec = _prepare_rollback_spec(
        RollbackSpecInput(
            incident_id="inc-1",
            root_cause="pr-42 broke GET /",
            evidence_summary="GET / fails since pr-42",
            confidence=0.9,
            justification="reverting pr-42 restores GET /",
            target_deploy=TargetDeploy.model_validate(suspect.model_dump()),
        )
    )
    assert isinstance(spec, RollbackSpec)
    assert spec.target_deploy.pr_number == record.pr_number
    assert spec.target_deploy.commit_sha == record.commit_sha
    assert spec.target_deploy.deployed_at == record.deployed_at


def test_deploy_correlation_builds_a_strict_output_schema() -> None:
    """Providers with structured outputs get DeployCorrelation as the output type."""
    schema = AgentOutputSchema(DeployCorrelation).json_schema()
    suspect = schema["$defs"]["CorrelatedDeploy"]
    assert {"pr_number", "commit_sha", "deployed_at", "version", "deploy_status"} <= set(
        suspect["properties"]
    )


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
    """Deploy correlator has exactly one tool — get_deploy_details."""
    assert len(correlator_agent.tools) == 1


def test_deploy_correlator_agent_tool_name(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert correlator_agent.tools[0].name == "get_deploy_details"


def test_deploy_correlator_agent_no_extra_tools(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    names = {t.name for t in correlator_agent.tools}
    unexpected = {
        "fetch_logs",
        "get_service_metadata",
        "search_past_incidents",
        "prepare_rollback_spec",
        "format_escalation",
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


def test_deploy_correlator_agent_instructions_mention_get_deploy_details(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert isinstance(correlator_agent.instructions, str)
    assert "get_deploy_details" in correlator_agent.instructions
    assert "list_recent_deploys" not in correlator_agent.instructions


def test_deploy_correlator_agent_uses_litellm_model(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert isinstance(correlator_agent.model, LitellmModel)


def test_deploy_correlator_agent_model_string_forwarded(
    pool: MagicMock,
    fake_settings: Settings,
) -> None:
    agent = build_deploy_correlator_agent(
        pool,
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "groq/llama-3.1-8b-instant"


def test_deploy_correlator_agent_custom_model_string(
    pool: MagicMock,
    fake_settings: Settings,
) -> None:
    agent = build_deploy_correlator_agent(
        pool,
        model_string="groq/llama-3.3-70b-versatile",
        settings=fake_settings,
    )
    assert isinstance(agent.model, LitellmModel)
    assert agent.model.model == "groq/llama-3.3-70b-versatile"


def test_deploy_correlator_agent_no_handoffs(
    correlator_agent: Agent[DeployCorrelation],
) -> None:
    assert len(correlator_agent.handoffs) == 0


def test_deploy_correlator_agent_accepts_no_pool(fake_settings: Settings) -> None:
    """Agent constructs without a pool; get_deploy_details reports "unavailable" at call time."""
    agent = build_deploy_correlator_agent(
        None,
        model_string="groq/llama-3.1-8b-instant",
        settings=fake_settings,
    )
    assert isinstance(agent, Agent)
    assert [t.name for t in agent.tools] == ["get_deploy_details"]
