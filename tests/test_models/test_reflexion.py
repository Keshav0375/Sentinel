"""Tests for sentinel.models.reflexion — the §4.3 reflexion output contract."""

from __future__ import annotations

from typing import Any

import pytest
from agents import AgentOutputSchema
from pydantic import ValidationError

from sentinel.models.reflexion import (
    ANALYSIS_TOOLS,
    REFLEXION_CONFIDENCE_THRESHOLD,
    DeployDetailsCall,
    FetchLogsCall,
    ReflexionResult,
)


def _sample(**overrides: Any) -> dict[str, Any]:
    """A low-confidence reply as the model would send it (JSON-shaped)."""
    reply: dict[str, Any] = {
        "confidence": 0.4,
        "supporting_evidence": ["get_deploy_details: pr-42 deployed 15 min before"],
        "missing_or_contradictory": ["no Synthetics run read after the deploy"],
        "additional_data_needed": ["Synthetics runs in the 60 min after the deploy"],
        "specific_deploy_identified": False,
        "deploy": None,
        "next_tool_call": {
            "tool": "fetch_logs",
            "args": {"service": "sentinel-watchtower", "window_minutes": 60},
        },
    }
    reply.update(overrides)
    return reply


def test_threshold_is_section_4_3() -> None:
    assert REFLEXION_CONFIDENCE_THRESHOLD == 0.7
    assert ANALYSIS_TOOLS == ("fetch_logs", "get_deploy_details")


def test_low_confidence_reply_with_fetch_logs_call() -> None:
    result = ReflexionResult.model_validate(_sample())
    assert result.needs_more_evidence
    assert isinstance(result.next_tool_call, FetchLogsCall)
    assert result.next_tool_call.args.service == "sentinel-watchtower"
    assert result.next_tool_call.args.include == ["synthetics", "deploys"]  # tool default


def test_low_confidence_reply_with_get_deploy_details_call() -> None:
    call = {"tool": "get_deploy_details", "args": {"service": "sentinel-watchtower"}}
    result = ReflexionResult.model_validate(_sample(next_tool_call=call))
    assert isinstance(result.next_tool_call, DeployDetailsCall)
    assert result.next_tool_call.args.window_hours == 24


def test_confident_reply_identifying_a_deploy() -> None:
    result = ReflexionResult.model_validate(
        _sample(
            confidence=0.85,
            specific_deploy_identified=True,
            deploy={"commit_sha": " a1b2c3d ", "pr_number": 42},
            next_tool_call=None,
        )
    )
    assert not result.needs_more_evidence
    assert result.deploy is not None
    assert (result.deploy.commit_sha, result.deploy.pr_number) == ("a1b2c3d", 42)


def test_direct_push_deploy_has_no_pr_number() -> None:
    result = ReflexionResult.model_validate(
        _sample(
            confidence=0.9,
            specific_deploy_identified=True,
            deploy={"commit_sha": "a1b2c3d", "pr_number": None},
        )
    )
    assert result.deploy is not None and result.deploy.pr_number is None


def test_confident_reply_drops_a_next_tool_call() -> None:
    """At or above the threshold no evidence is requested, whatever the model added."""
    result = ReflexionResult.model_validate(_sample(confidence=0.7))
    assert result.next_tool_call is None


@pytest.mark.parametrize(
    "tool", ["prepare_rollback_spec", "format_escalation", "get_service_metadata", "rm_rf"]
)
def test_next_tool_call_is_restricted_to_analysis_tools(tool: str) -> None:
    call = {"tool": tool, "args": {"service": "sentinel-watchtower"}}
    with pytest.raises(ValidationError):
        ReflexionResult.model_validate(_sample(next_tool_call=call))


def test_next_tool_call_args_must_fit_the_named_tool() -> None:
    call = {"tool": "fetch_logs", "args": {"window_minutes": 60}}  # no service
    with pytest.raises(ValidationError):
        ReflexionResult.model_validate(_sample(next_tool_call=call))


def test_low_confidence_requires_next_tool_call() -> None:
    with pytest.raises(ValidationError, match="requires next_tool_call"):
        ReflexionResult.model_validate(_sample(next_tool_call=None))


@pytest.mark.parametrize("confidence", [-0.1, 1.01, 7])
def test_confidence_outside_unit_interval_is_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError, match="between 0.0 and 1.0"):
        ReflexionResult.model_validate(_sample(confidence=confidence))


def test_identified_deploy_requires_the_deploy() -> None:
    with pytest.raises(ValidationError, match="deploy is null"):
        ReflexionResult.model_validate(_sample(confidence=0.9, specific_deploy_identified=True))


def test_deploy_without_identification_is_rejected() -> None:
    with pytest.raises(ValidationError, match="specific_deploy_identified is false"):
        ReflexionResult.model_validate(_sample(deploy={"commit_sha": "a1b2c3d", "pr_number": 42}))


def test_blank_commit_sha_is_rejected() -> None:
    with pytest.raises(ValidationError, match="blank"):
        ReflexionResult.model_validate(
            _sample(
                confidence=0.9,
                specific_deploy_identified=True,
                deploy={"commit_sha": "  ", "pr_number": 42},
            )
        )


def test_every_field_is_required() -> None:
    """The model must answer every field: none silently defaults."""
    for field in ReflexionResult.model_fields:
        reply = _sample()
        del reply[field]
        with pytest.raises(ValidationError):
            ReflexionResult.model_validate(reply)


def test_usable_as_a_strict_agent_output_type() -> None:
    schema = AgentOutputSchema(ReflexionResult)
    assert schema.is_strict_json_schema()
    result = schema.validate_json(ReflexionResult.model_validate(_sample()).model_dump_json())
    assert isinstance(result, ReflexionResult)
