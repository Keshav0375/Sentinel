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
def test_next_tool_call_outside_the_analysis_tools_is_dropped(tool: str) -> None:
    """Only an analysis tool can be named; anything else is dropped, the critique kept."""
    call = {"tool": tool, "args": {"service": "sentinel-watchtower"}}
    result = ReflexionResult.model_validate(_sample(next_tool_call=call))
    assert result.next_tool_call is None
    assert result.confidence == 0.4
    assert result.needs_more_evidence


def test_next_tool_call_args_that_do_not_fit_are_dropped() -> None:
    call = {"tool": "fetch_logs", "args": {"window_minutes": 60}}  # no service
    result = ReflexionResult.model_validate(_sample(next_tool_call=call))
    assert result.next_tool_call is None


def test_low_confidence_without_next_tool_call_is_valid() -> None:
    """The orchestrator loops without guidance, or escalates — the reply is not discarded."""
    result = ReflexionResult.model_validate(_sample(next_tool_call=None))
    assert result.needs_more_evidence
    assert result.next_tool_call is None
    assert result.supporting_evidence == ["get_deploy_details: pr-42 deployed 15 min before"]


@pytest.mark.parametrize(
    ("confidence", "clamped"),
    [(-0.1, 0.0), (1.01, 1.0), (7, 1.0), (float("nan"), 0.0), ("0.85", 0.85)],
)
def test_confidence_is_clamped_to_the_unit_interval(confidence: Any, clamped: float) -> None:
    result = ReflexionResult.model_validate(_sample(confidence=confidence))
    assert result.confidence == clamped


def test_confidence_that_is_not_a_number_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ReflexionResult.model_validate(_sample(confidence="high"))


def test_identified_without_a_deploy_is_repaired_to_not_identified() -> None:
    result = ReflexionResult.model_validate(
        _sample(confidence=0.9, specific_deploy_identified=True, deploy=None)
    )
    assert result.specific_deploy_identified is False
    assert result.confidence == 0.9


def test_an_explicit_false_wins_over_a_deploy() -> None:
    """A repair never opens the gate: a deploy does not turn the model's false into true."""
    result = ReflexionResult.model_validate(
        {
            "confidence": 0.9,
            "specific_deploy_identified": False,
            "deploy": {"commit_sha": "abcdef1", "pr_number": 4},
        }
    )
    assert result.specific_deploy_identified is False
    assert result.confidence == 0.9
    assert result.deploy is not None and result.deploy.commit_sha == "abcdef1"  # kept, unused


@pytest.mark.parametrize("claimed", [None, "true", 1])
def test_anything_but_an_explicit_true_is_not_identified(claimed: Any) -> None:
    reply = _sample(confidence=0.9, deploy={"commit_sha": "abcdef1", "pr_number": 4})
    if claimed is None:
        del reply["specific_deploy_identified"]
    else:
        reply["specific_deploy_identified"] = claimed
    assert ReflexionResult.model_validate(reply).specific_deploy_identified is False


def test_identified_with_a_deploy_stays_identified() -> None:
    result = ReflexionResult.model_validate(
        _sample(
            confidence=0.85,
            specific_deploy_identified=True,
            deploy={"commit_sha": "a1b2c3d", "pr_number": 42},
        )
    )
    assert result.specific_deploy_identified is True


def test_blank_commit_sha_drops_the_deploy_and_keeps_the_confidence() -> None:
    result = ReflexionResult.model_validate(
        _sample(
            confidence=0.9,
            specific_deploy_identified=True,
            deploy={"commit_sha": "  ", "pr_number": 42},
        )
    )
    assert result.deploy is None
    assert result.specific_deploy_identified is False
    assert result.confidence == 0.9


def test_constructor_repairs_like_validation() -> None:
    result = ReflexionResult(
        confidence=1.5,
        supporting_evidence=[],
        missing_or_contradictory=[],
        additional_data_needed=[],
        specific_deploy_identified=True,
        deploy=None,
        next_tool_call=None,
    )
    assert (result.confidence, result.specific_deploy_identified) == (1.0, False)


def test_only_confidence_is_needed_to_parse() -> None:
    """An omitted list is empty and an omitted deploy or call is null: never discarded."""
    result = ReflexionResult.model_validate({"confidence": 0.75})
    assert result.confidence == 0.75
    assert result.supporting_evidence == []
    assert result.deploy is None and result.specific_deploy_identified is False
    assert result.next_tool_call is None
    with pytest.raises(ValidationError):
        ReflexionResult.model_validate({"deploy": None})


def test_the_schema_still_asks_for_every_field() -> None:
    """Tolerant to parse, strict to ask: the model is told every field is required."""
    schema = ReflexionResult.model_json_schema()
    assert set(schema["required"]) == set(ReflexionResult.model_fields)


def test_usable_as_a_strict_agent_output_type() -> None:
    schema = AgentOutputSchema(ReflexionResult)
    assert schema.is_strict_json_schema()
    result = schema.validate_json(ReflexionResult.model_validate(_sample()).model_dump_json())
    assert isinstance(result, ReflexionResult)
