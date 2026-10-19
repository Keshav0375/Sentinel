"""Tests for sentinel.agents.orchestrator — the §4.2 pipeline as Python code.

Every run is on the fake LLM (scripted per agent name; no key, no network): the real
agents, tools, budget guard and fallback run. The rollback-target cross-check is stubbed
here except in the Postgres tests at the end (TEST_DATABASE_URL), which check it against
real ``deployments`` rows.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import litellm
import pytest
from agents import FunctionTool, function_tool
from agents.tool_context import ToolContext
from agents.tracing import get_trace_provider
from alembic import command
from alembic.config import Config

from sentinel.agents import orchestrator
from sentinel.agents.orchestrator import (
    PIPELINE_ERROR,
    REFLEXION_MAX_LOOPS,
    REFLEXION_UNPARSEABLE,
    PipelineDeps,
    ToolBudget,
    budgeted,
    run_pipeline,
)
from sentinel.config import Settings
from sentinel.infra import tracing
from sentinel.infra.database import create_pool
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.analysis import AnalysisResult
from sentinel.models.deploy import CorrelatedDeploy
from sentinel.models.incident import Severity, TriageResult
from sentinel.models.pipeline import PipelineInput, PipelineResult, SignalType
from sentinel.models.reflexion import FetchLogsCall, SuspectDeploy
from sentinel.models.tools import DeployRecord, LogFetchInput, TargetDeploy
from sentinel.providers import (
    REFLEXION_AGENT,
    REVERIFICATION_AGENT,
    FakeScript,
    FakeToolCall,
    FakeTurn,
    fake_script,
    reflexion_turn,
    reverification_turn,
)
from sentinel.tools.deploy_checker import TargetDeployCheck

SERVICE = "sentinel-watchtower"
SHA = "a1b2c3d"
PR = 42
DEPLOYED_AT = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
DEPLOY = SuspectDeploy(commit_sha=SHA, pr_number=PR)
HAIKU = "anthropic/claude-haiku-4-5"
SONNET = "anthropic/claude-sonnet-4-6"
MINI = "openai/gpt-4o-mini"


# ── Fixtures and script builders ──────────────────────────────────────────────


def _deps(pool: asyncpg.Pool | None = None, **settings: Any) -> PipelineDeps:
    return PipelineDeps(
        settings=Settings(_env_file=None, sentinel_fake_llm=True, **settings),  # type: ignore[call-arg]
        semantic_memory=SemanticMemory(None),
        episodic_memory=EpisodicMemory(None, MagicMock()),
        pool=pool,
    )


def _request(signal_type: SignalType = "runtime_error") -> PipelineInput:
    alert = AlertPayload(
        source=AlertSource.DATADOG,
        service=SERVICE,
        metric="synthetics.http.failed",
        threshold=1,
        current_value=3,
        severity=AlertSeverity.CRITICAL,
    )
    return PipelineInput(
        incident_id=str(uuid.uuid4()),
        alert=alert,
        signal_type=signal_type,
        correlation_id=str(uuid.uuid4()),
    )


def _triage(usage: tuple[int, int] | None = None) -> FakeTurn:
    return FakeTurn(
        output=TriageResult(
            severity=Severity.P2,
            affected_service=SERVICE,
            is_duplicate=False,
            recommended_action="check the last deploy",
        ),
        usage=usage,
    )


def _analysis(
    hypothesis: str = "GET / returns HTML since pr-42", usage: tuple[int, int] | None = None
) -> list[FakeTurn]:
    """One analysis pass: a fetch_logs call, then the answer."""
    suspect = CorrelatedDeploy(
        pr_number=PR, commit_sha=SHA, deployed_at=DEPLOYED_AT, deploy_status="succeeded"
    )
    return [
        FakeTurn(
            tool_calls=[FakeToolCall("fetch_logs", {"request": {"service": SERVICE}})],
            usage=usage,
        ),
        FakeTurn(
            output=AnalysisResult(
                hypothesis=hypothesis,
                evidence=["fetch_logs: every check failed after pr-42"],
                suspect_deploy=suspect,
            ),
            usage=usage,
        ),
    ]


def _low(window_minutes: int = 90) -> FakeTurn:
    return reflexion_turn(
        0.4,
        next_tool_call=FetchLogsCall(
            tool="fetch_logs",
            args=LogFetchInput(service=SERVICE, window_minutes=window_minutes),
        ),
        missing_or_contradictory=["no check read after the deploy"],
    )


def _high(usage: tuple[int, int] | None = None) -> FakeTurn:
    return reflexion_turn(
        0.85, deploy=DEPLOY, supporting_evidence=["checks fail since pr-42"], usage=usage
    )


def _rollback(
    request: PipelineInput,
    *,
    sha: str = SHA,
    pr: int = PR,
    deployed_at: datetime = DEPLOYED_AT,
    confidence: float = 0.9,
    usage: tuple[int, int] | None = None,
) -> list[FakeTurn]:
    """One resolution run: prepare_rollback_spec, then a one-line reply."""
    spec = {
        "incident_id": request.incident_id,
        "root_cause": "pr-42 changed GET / to return HTML",
        "evidence_summary": "every check failed after pr-42 deployed",
        "confidence": confidence,
        "justification": "the previous version passed the same checks",
        "target_deploy": {
            "pr_number": pr,
            "commit_sha": sha,
            "deployed_at": deployed_at.isoformat(),
        },
    }
    return [
        FakeTurn(
            tool_calls=[FakeToolCall("prepare_rollback_spec", {"request": spec})], usage=usage
        ),
        FakeTurn(output="Drafted the rollback of PR #42.", usage=usage),
    ]


def _happy(script: FakeScript, request: PipelineInput) -> None:
    script.on("triage", _triage())
    script.on("analysis", *_analysis())
    script.on(REFLEXION_AGENT, _high())
    script.on("resolution", *_rollback(request))
    script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))


def _runs(script: FakeScript, agent: str) -> int:
    """How many model calls the agent made."""
    return sum(1 for name, _ in script.calls if name == agent)


@pytest.fixture
def target_ok(monkeypatch: pytest.MonkeyPatch) -> list[TargetDeploy]:
    """The cross-check finds a matching row for whatever target it is given."""
    seen: list[TargetDeploy] = []

    async def check(
        pool: asyncpg.Pool | None, service: str, target: TargetDeploy
    ) -> TargetDeployCheck:
        seen.append(target)
        record = DeployRecord(
            pr_number=target.pr_number,
            commit_sha=target.commit_sha,
            deploy_status="succeeded",
            deployed_at=target.deployed_at,
            files_changed=["app/main.py"],
        )
        return TargetDeployCheck(deploy_id="deploy-1", record=record)

    monkeypatch.setattr(orchestrator, "check_rollback_target", check)
    return seen


@pytest.fixture
def inputs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """(agent name, input text) of every agent call, in order."""
    seen: list[tuple[str, str]] = []
    real = orchestrator.call_with_fallback

    async def spy(agent: Any, input: Any, **kwargs: Any) -> Any:
        seen.append((agent.name, str(input)))
        return await real(agent, input, **kwargs)

    monkeypatch.setattr(orchestrator, "call_with_fallback", spy)
    return seen


@pytest.fixture
def bridge() -> Iterator[None]:
    """The R14 generation bridge installed (it sums per-incident tokens), then removed."""
    multi: Any = get_trace_provider()._multi_processor  # type: ignore[attr-defined]
    before = list(multi._processors)
    tracing.install_trace_processors()
    try:
        yield
    finally:
        multi.set_processors(before)


# ── Rollback path ─────────────────────────────────────────────────────────────


async def test_high_confidence_specific_deploy_proposes_the_rollback(
    target_ok: list[TargetDeploy],
) -> None:
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _deps())

    assert result.status == "rollback_proposed"
    assert result.resolution_type == "rollback"
    assert result.escalation is None
    spec = result.rollback_spec
    assert spec is not None and spec.incident_id == request.incident_id
    assert spec.target_deploy.commit_sha == SHA and spec.target_deploy.pr_number == PR
    assert result.target_sha == SHA
    assert result.root_cause == spec.root_cause and result.confidence == 0.9
    assert result.evidence_summary == spec.evidence_summary
    assert result.reflexion_loops == 0
    assert result.tool_calls_used == 2  # fetch_logs + prepare_rollback_spec
    assert result.models_used == [HAIKU, SONNET]
    assert [t.commit_sha for t in target_ok] == [SHA]
    assert _runs(script, REVERIFICATION_AGENT) == 1


async def test_the_result_validates_as_a_round_trip(target_ok: list[TargetDeploy]) -> None:
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _deps())
    assert PipelineResult.model_validate_json(result.model_dump_json()) == result


async def test_the_reported_token_total_is_every_llm_call(
    bridge: None, target_ok: list[TargetDeploy]
) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage(usage=(100, 10)))
        script.on("analysis", *_analysis(usage=(200, 20)))  # two model calls
        script.on(REFLEXION_AGENT, _high(usage=(300, 30)))
        script.on("resolution", *_rollback(request, usage=(400, 40)))  # two model calls
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS", usage=(500, 50)))
        result = await run_pipeline(request, _deps())

    assert result.status == "rollback_proposed"
    assert result.tokens_used == 110 + 2 * 220 + 330 + 2 * 440 + 550


# ── Reflexion loop ────────────────────────────────────────────────────────────


async def test_low_confidence_loops_exactly_twice_then_escalates(
    target_ok: list[TargetDeploy],
) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis(), *_analysis(), *_analysis())
        script.on(REFLEXION_AGENT, _low(), _low(), _low())
        result = await run_pipeline(request, _deps())

    assert REFLEXION_MAX_LOOPS == 2
    assert result.status == "escalated" and result.resolution_type == "escalated"
    assert result.reflexion_loops == 2
    assert _runs(script, REFLEXION_AGENT) == 3  # the first critique + one per loop
    assert _runs(script, "analysis") == 6  # three passes of (tool call, answer)
    assert _runs(script, "resolution") == 0
    assert target_ok == []
    escalation = result.escalation
    assert escalation is not None and result.rollback_spec is None
    assert escalation.missing[0].startswith("decision gate: confidence 0.40")
    assert "no check read after the deploy" in escalation.missing
    assert escalation.hypothesis == "GET / returns HTML since pr-42"
    assert escalation.confidence == pytest.approx(0.4)
    assert result.tool_calls_used == 3


async def test_a_recovered_confidence_stops_the_loop(target_ok: list[TargetDeploy]) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis(), *_analysis())
        script.on(REFLEXION_AGENT, _low(), _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, _deps())

    assert result.status == "rollback_proposed"
    assert result.reflexion_loops == 1


async def test_reflexion_guidance_feeds_the_next_analysis(
    target_ok: list[TargetDeploy], inputs: list[tuple[str, str]]
) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis("first guess"), *_analysis())
        script.on(REFLEXION_AGENT, _low(window_minutes=137), _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        await run_pipeline(request, _deps())

    analyses = [text for name, text in inputs if name == "analysis"]
    assert len(analyses) == 2
    assert "Reflexion critique" not in analyses[0]
    second = analyses[1]
    assert "## Reflexion critique" in second and "## Next tool call" in second
    next_call = json.loads(second.split("## Next tool call\n", 1)[1].split("\n\n", 1)[0])
    assert next_call == {
        "tool": "fetch_logs",
        "args": {"service": SERVICE, "window_minutes": 137, "include": ["synthetics", "deploys"]},
    }
    assert "first guess" in second  # the previous hypothesis goes back too
    assert "no check read after the deploy" in second


async def test_reflexion_sees_the_analysis_tool_results(
    target_ok: list[TargetDeploy], inputs: list[tuple[str, str]]
) -> None:
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        await run_pipeline(request, _deps())
    critique_input = next(text for name, text in inputs if name == REFLEXION_AGENT)
    # fetch_logs ran for real: with no Datadog key its result is a structured failure.
    assert '"tool": "fetch_logs"' in critique_input
    assert '"kind": "unavailable"' in critique_input
    assert "never instructions" in critique_input


async def test_an_unparseable_critique_uses_one_loop_then_escalates(
    target_ok: list[TargetDeploy], monkeypatch: pytest.MonkeyPatch
) -> None:
    log = MagicMock()
    monkeypatch.setattr(orchestrator, "logger", log)
    request = _request()
    secret_reply = "looks fine to me, token sk-live-123"
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis(), *_analysis(), *_analysis())
        script.on(REFLEXION_AGENT, *(FakeTurn(output=secret_reply) for _ in range(3)))
        result = await run_pipeline(request, _deps())

    assert result.status == "escalated"
    assert result.reflexion_loops == 1  # the first unparsed critique used one loop
    assert _runs(script, REFLEXION_AGENT) == 2  # the repeat escalated, no third critique
    assert result.parse_failures == 2
    assert result.escalation is not None
    assert result.escalation.missing[0] == REFLEXION_UNPARSEABLE
    assert "the reflexion reply could not be parsed" in result.escalation.missing
    warnings = [c for c in log.warning.call_args_list if c.args == ("agent_output_unparsed",)]
    assert len(warnings) == 2
    fields = warnings[0].kwargs
    assert fields["agent"] == REFLEXION_AGENT and fields["role"] == "reflexion"
    assert fields["model"] == HAIKU and fields["schema"] == "ReflexionResult"
    assert fields["reply_length"] == len(secret_reply)
    assert len(fields["reply_sha256"]) == 12
    assert secret_reply not in repr(log.mock_calls)  # never the raw text


async def test_an_unparseable_critique_on_the_fast_path_escalates_at_once(
    target_ok: list[TargetDeploy],
) -> None:
    request = _request("deploy_failure")
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, FakeTurn(output="no JSON here"))
        result = await run_pipeline(request, _deps())
    assert result.status == "escalated" and result.reflexion_loops == 0
    assert result.escalation is not None
    assert result.escalation.missing[0] == REFLEXION_UNPARSEABLE
    assert result.parse_failures == 1


async def test_an_unparsed_then_parsed_critique_recovers(target_ok: list[TargetDeploy]) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis(), *_analysis())
        script.on(REFLEXION_AGENT, FakeTurn(output="thinking..."), _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, _deps())
    assert result.status == "rollback_proposed"
    assert (result.reflexion_loops, result.parse_failures) == (1, 1)


async def test_unparsed_triage_and_analysis_are_counted(
    target_ok: list[TargetDeploy], inputs: list[tuple[str, str]]
) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", FakeTurn(output="P2, I think"))
        script.on(
            "analysis",
            FakeTurn(tool_calls=[FakeToolCall("fetch_logs", {"request": {"service": SERVICE}})]),
            FakeTurn(output="pr-42 broke it"),
        )
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, _deps())
    assert result.parse_failures == 2
    assert result.status == "rollback_proposed"  # the critique carried the gate
    critique_input = next(text for name, text in inputs if name == REFLEXION_AGENT)
    assert "pr-42 broke it" in critique_input  # the raw reply stands in as the hypothesis


async def test_low_confidence_without_a_next_call_loops_without_guidance(
    target_ok: list[TargetDeploy], inputs: list[tuple[str, str]]
) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis(), *_analysis())
        script.on(
            REFLEXION_AGENT,
            reflexion_turn(0.5, missing_or_contradictory=["no Synthetics run read"]),
            _high(),
        )
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("PASS"))
        result = await run_pipeline(request, _deps())
    assert result.status == "rollback_proposed" and result.reflexion_loops == 1
    second = [text for name, text in inputs if name == "analysis"][1]
    assert "without naming a tool call" in second
    assert "## Next tool call" not in second
    assert "no Synthetics run read" in second


async def test_high_confidence_without_a_specific_deploy_escalates(
    target_ok: list[TargetDeploy],
) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, reflexion_turn(0.9))  # no deploy identified
        result = await run_pipeline(request, _deps())
    assert result.status == "escalated" and result.reflexion_loops == 0
    assert result.escalation is not None
    assert "one specific deploy" in result.escalation.missing[0]
    assert _runs(script, "resolution") == 0


# ── Reverification ────────────────────────────────────────────────────────────


async def test_reverify_fail_retries_resolution_once_then_passes(
    target_ok: list[TargetDeploy], inputs: list[tuple[str, str]]
) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request), *_rollback(request))
        script.on(
            REVERIFICATION_AGENT,
            reverification_turn("FAIL", "the evidence summary omits the failed assertion"),
            reverification_turn("PASS"),
        )
        result = await run_pipeline(request, _deps())

    assert result.status == "rollback_proposed"
    assert _runs(script, REVERIFICATION_AGENT) == 2
    resolutions = [text for name, text in inputs if name == "resolution"]
    assert len(resolutions) == 2
    assert "Correction from reverification" not in resolutions[0]
    assert "the evidence summary omits the failed assertion" in resolutions[1]


async def test_reverify_fail_twice_escalates(target_ok: list[TargetDeploy]) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request), *_rollback(request))
        script.on(
            REVERIFICATION_AGENT,
            reverification_turn("FAIL", "wrong target"),
            reverification_turn("FAIL", "still the wrong target"),
        )
        result = await run_pipeline(request, _deps())

    assert result.status == "escalated"
    assert _runs(script, "resolution") == 4  # two runs of (tool call, reply)
    assert result.escalation is not None
    assert result.escalation.missing[0] == (
        "reverification failed after a retry: still the wrong target"
    )


async def test_reverify_escalate_escalates_without_a_retry(
    target_ok: list[TargetDeploy],
) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request), *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("ESCALATE", "failures predate pr-42"))
        result = await run_pipeline(request, _deps())

    assert result.status == "escalated"
    assert _runs(script, "resolution") == 2  # one run
    assert result.escalation is not None
    assert result.escalation.missing[0] == "reverification escalated: failures predate pr-42"


async def test_a_malformed_reverification_reply_escalates(
    target_ok: list[TargetDeploy],
) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request))
        script.on(REVERIFICATION_AGENT, reverification_turn("looks good"))
        result = await run_pipeline(request, _deps())
    assert result.status == "escalated"


async def test_resolution_drafting_an_escalation_escalates_with_its_context(
    target_ok: list[TargetDeploy],
) -> None:
    request = _request()
    escalation = {
        "hypothesis": "a config change, not the deploy",
        "evidence": ["the diff only touches docs"],
        "confidence": 0.7,
        "missing": ["the runtime config"],
    }
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on(
            "resolution",
            FakeTurn(tool_calls=[FakeToolCall("format_escalation", {"request": escalation})]),
            FakeTurn(output="Escalated."),
        )
        result = await run_pipeline(request, _deps())
    assert result.status == "escalated"
    assert result.escalation is not None
    assert result.escalation.hypothesis == "a config change, not the deploy"
    assert result.root_cause == "a config change, not the deploy"
    assert _runs(script, REVERIFICATION_AGENT) == 0


async def test_no_draft_or_a_low_confidence_spec_escalates(
    target_ok: list[TargetDeploy],
) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", FakeTurn(output="I would rather not."))
        result = await run_pipeline(request, _deps())
    assert result.escalation is not None
    assert result.escalation.missing[0] == "the resolution agent drafted no rollback spec"

    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis())
        script.on(REFLEXION_AGENT, _high())
        script.on("resolution", *_rollback(request, confidence=0.6))
        result = await run_pipeline(request, _deps())
    assert result.escalation is not None
    assert "confidence 0.60 is below 0.7" in result.escalation.missing[0]
    assert target_ok == []  # never cross-checked, never reverified


async def test_a_cross_check_problem_escalates_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def mismatch(
        pool: asyncpg.Pool | None, service: str, target: TargetDeploy
    ) -> TargetDeployCheck:
        return TargetDeployCheck(problem="no deployments row for svc has commit a1b2c3d")

    monkeypatch.setattr(orchestrator, "check_rollback_target", mismatch)
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _deps())
    assert result.status == "escalated"
    assert result.escalation is not None
    assert result.escalation.missing[0] == "no deployments row for svc has commit a1b2c3d"
    assert _runs(script, REVERIFICATION_AGENT) == 0


async def test_without_a_database_no_rollback_can_be_proposed() -> None:
    """The real cross-check with no pool: the target cannot be checked → escalate."""
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _deps(pool=None))
    assert result.status == "escalated"
    assert result.escalation is not None
    assert "no database is configured" in result.escalation.missing[0]


# ── deploy_failure fast path ──────────────────────────────────────────────────


async def test_deploy_failure_fast_path_is_gated_and_reverified(
    target_ok: list[TargetDeploy], inputs: list[tuple[str, str]]
) -> None:
    request = _request("deploy_failure")
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _deps())

    assert result.status == "rollback_proposed"
    assert result.reflexion_loops == 0
    assert _runs(script, REVERIFICATION_AGENT) == 1
    assert target_ok and target_ok[0].commit_sha == SHA
    analysis_input = next(text for name, text in inputs if name == "analysis")
    assert "Signal type: deploy_failure" in analysis_input


async def test_deploy_failure_low_confidence_escalates_without_a_loop(
    target_ok: list[TargetDeploy],
) -> None:
    request = _request("deploy_failure")
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis(), *_analysis())
        script.on(REFLEXION_AGENT, _low(), _high())
        result = await run_pipeline(request, _deps())

    assert result.status == "escalated"
    assert result.reflexion_loops == 0
    assert _runs(script, REFLEXION_AGENT) == 1
    assert _runs(script, "analysis") == 2  # one pass only
    assert _runs(script, "resolution") == 0


# ── Tool-call budget (§4.6) ───────────────────────────────────────────────────


def _calls(name: str, count: int, args: dict[str, Any]) -> list[FakeToolCall]:
    return [FakeToolCall(name, {"request": args}) for _ in range(count)]


async def test_the_budget_stops_at_20_across_parallel_calls_and_a_fallback() -> None:
    request = _request()
    service_args = {"service_name": SERVICE}
    search_args = {"symptom_text": "checks failing"}
    logs_args = {"service": SERVICE}
    with fake_script() as script:
        script.on(
            "triage",
            # Primary: 4 parallel calls, then a provider timeout → re-run on the fallback.
            FakeTurn(
                tool_calls=[
                    *_calls("get_service_metadata", 2, service_args),
                    *_calls("search_past_incidents", 2, search_args),
                ]
            ),
            FakeTurn(error=TimeoutError("primary timed out")),
            # Fallback re-run: 4 more calls, then the answer.
            FakeTurn(tool_calls=_calls("get_service_metadata", 4, service_args)),
            _triage(),
        )
        script.on(
            "analysis",
            FakeTurn(tool_calls=_calls("fetch_logs", 5, logs_args)),  # 13
            FakeTurn(tool_calls=_calls("fetch_logs", 5, logs_args)),  # 18
            FakeTurn(tool_calls=_calls("fetch_logs", 5, logs_args)),  # 20, 3 refused
            *_analysis(),  # never reached: the run stops before the next model call
        )
        result = await run_pipeline(request, _deps())

    assert result.tool_calls_used == 20
    assert result.status == "escalated"
    assert _runs(script, "analysis") == 3
    assert _runs(script, REFLEXION_AGENT) == 0
    escalation = result.escalation
    assert escalation is not None
    assert escalation.missing[0] == (
        "the tool-call budget (20 per incident) was exhausted during analysis; "
        "3 further call(s) were refused"
    )
    # Gathered context: with no analysis answer, the tool results gathered so far.
    assert escalation.evidence and all(e.startswith("fetch_logs: ") for e in escalation.evidence)
    assert MINI in result.models_used  # triage fell back
    assert HAIKU not in result.models_used


async def test_the_budget_is_settings_driven() -> None:
    request = _request()
    with fake_script() as script:
        script.on(
            "triage",
            FakeTurn(tool_calls=_calls("get_service_metadata", 3, {"service_name": SERVICE})),
            _triage(),
        )
        result = await run_pipeline(request, _deps(sentinel_max_tool_calls=2))
    assert result.tool_calls_used == 2
    assert result.escalation is not None
    assert result.escalation.missing[0].startswith(
        "the tool-call budget (2 per incident) was exhausted during triage"
    )
    assert _runs(script, "triage") == 1


def test_tool_budget_counts_and_refuses() -> None:
    budget = ToolBudget(2)
    assert [budget.take() for _ in range(4)] == [True, True, False, False]
    assert (budget.used, budget.refused, budget.exhausted) == (2, 2, True)
    with pytest.raises(ValueError):
        ToolBudget(0)


async def test_budgeted_tool_refuses_without_running_the_tool() -> None:
    ran: list[str] = []

    @function_tool
    def ping(text: str) -> str:
        ran.append(text)
        return f"pong {text}"

    seen: list[tuple[str, str, bool]] = []
    budget = ToolBudget(1)
    tool = budgeted(ping, budget, lambda *event: seen.append(event))
    assert isinstance(tool, FunctionTool) and tool.name == "ping"

    def context() -> ToolContext[Any]:
        return ToolContext(context=None, tool_name="ping", tool_call_id="c", tool_arguments="{}")

    first, second = await asyncio.gather(
        tool.on_invoke_tool(context(), '{"text": "a"}'),
        tool.on_invoke_tool(context(), '{"text": "b"}'),
    )
    assert first == "pong a" and ran == ["a"]
    refusal = json.loads(second)
    assert refusal["error"] is True and refusal["kind"] == "unavailable"
    assert "budget exhausted" in refusal["message"]
    assert sorted(seen, key=lambda event: event[2]) == [
        ("ping", "pong a", False),
        ("ping", second, True),
    ]


# ── Providers and failures ────────────────────────────────────────────────────


async def test_a_provider_fallback_mid_pipeline_shows_in_models_used(
    target_ok: list[TargetDeploy],
) -> None:
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        script.fail_on(HAIKU, litellm.RateLimitError("429", llm_provider="anthropic", model=HAIKU))
        result = await run_pipeline(request, _deps())

    # triage, reflexion and reverification run on Haiku: each fell back to gpt-4o-mini.
    assert result.status == "rollback_proposed"
    assert result.models_used == [MINI, SONNET]
    assert (REVERIFICATION_AGENT, HAIKU) in script.calls
    assert (REVERIFICATION_AGENT, MINI) in script.calls


async def test_a_pipeline_exception_escalates(target_ok: list[TargetDeploy]) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", FakeTurn(error=ValueError("model returned garbage")))
        result = await run_pipeline(request, _deps())

    assert result.status == "escalated" and result.resolution_type == "escalated"
    assert result.escalation is not None
    assert result.escalation.missing[0] == f"{PIPELINE_ERROR} during analysis: ValueError"
    assert result.models_used == [HAIKU]


async def test_no_provider_key_escalates_without_a_tool_call() -> None:
    deps = PipelineDeps(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        semantic_memory=SemanticMemory(None),
        episodic_memory=EpisodicMemory(None, MagicMock()),
    )
    result = await run_pipeline(_request(), deps)
    assert result.status == "escalated"
    assert result.escalation is not None
    assert result.escalation.missing[0] == (
        f"{PIPELINE_ERROR} during triage: ProviderUnavailableError"
    )
    assert result.tool_calls_used == 0 and result.models_used == []


async def test_unscripted_fake_run_ends_escalated() -> None:
    """SENTINEL_FAKE_LLM=1 with no script (app boot, eval smoke): a clean escalation."""
    with fake_script():
        result = await run_pipeline(_request(), _deps())
    assert result.status == "escalated"
    assert result.reflexion_loops == REFLEXION_MAX_LOOPS
    assert result.parse_failures == 4  # triage and three analyses answer plain text


# ── Rollback-target cross-check against real deployments rows (Postgres) ──────


def _with_database(dsn: str, name: str) -> str:
    return urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))


def _execute(dsn: str, sql: str) -> None:
    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_go())


@pytest.fixture(scope="module")
def _module_db() -> Iterator[dict[str, str]]:
    holder: dict[str, str] = {}
    yield holder
    if holder:
        _execute(holder["server"], f'DROP DATABASE IF EXISTS "{holder["name"]}" WITH (FORCE)')


@pytest.fixture
def pipeline_dsn(
    pg_dsn: str, _module_db: dict[str, str], alembic_config: Callable[..., Config]
) -> str:
    """A database at migration 001 (the §5.3 schema), with no deployments or incidents."""
    if not _module_db:
        name = f"sentinel_pipeline_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"]), "001")
    _execute(_module_db["dsn"], "DELETE FROM deployments; DELETE FROM incidents")
    return _module_db["dsn"]


@pytest.fixture
async def pool(pipeline_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(pipeline_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def _deploy_row(
    pool: asyncpg.Pool,
    *,
    sha: str = "a1b2c3d4e5f6",
    pr: int = PR,
    deployed_at: datetime = DEPLOYED_AT,
    status: str = "succeeded",
    service: str = SERVICE,
) -> None:
    await pool.execute(
        """
        INSERT INTO deployments (service, pr_number, commit_sha, deploy_status, deployed_at,
                                 files_changed, metadata)
        VALUES ($1, $2, $3, $4, $5, '["app/main.py"]'::jsonb,
                jsonb_build_object('failed_stage', 'none', 'version', 'pr-42-a1b2c3d'))
        """,
        service,
        pr,
        sha,
        status,
        deployed_at,
    )


async def test_a_real_matching_row_lets_the_rollback_through(
    pool: asyncpg.Pool, inputs: list[tuple[str, str]]
) -> None:
    await _deploy_row(pool, status="failed")  # failed rows are valid targets
    request = _request("deploy_failure")
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _deps(pool))

    assert result.status == "rollback_proposed"
    reverify = next(text for name, text in inputs if name == REVERIFICATION_AGENT)
    target = json.loads(reverify.split("## Target deploy\n", 1)[1].split("\n\n", 1)[0])
    assert target["commit_sha"] == "a1b2c3d4e5f6" and target["deploy_status"] == "failed"
    assert target["files_changed"] == ["app/main.py"] and target["service"] == SERVICE


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        ({"sha": "fffffff1234"}, "no deployments row for sentinel-watchtower has commit a1b2c3d"),
        ({"service": "other-service"}, "no deployments row for sentinel-watchtower"),
        ({"pr": 41}, "was recorded as PR #41"),
        ({"deployed_at": DEPLOYED_AT - timedelta(minutes=5)}, "but the rollback spec says PR #42"),
    ],
)
async def test_a_mismatched_or_missing_row_escalates(
    pool: asyncpg.Pool, row: dict[str, Any], problem: str
) -> None:
    await _deploy_row(pool, **row)
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _deps(pool))

    assert result.status == "escalated"
    assert result.escalation is not None
    assert problem in result.escalation.missing[0]
    assert _runs(script, REVERIFICATION_AGENT) == 0


# ── Persistence (store_incident with the outcome) ─────────────────────────────


class _Embedder:
    """A 384-dim embedding client stand-in: one fixed non-zero vector, no model load."""

    async def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 383


def _stored_deps(pool: asyncpg.Pool) -> PipelineDeps:
    deps = _deps(pool)
    return dataclasses.replace(deps, episodic_memory=EpisodicMemory(pool, _Embedder()))  # type: ignore[arg-type]


async def _incident_row(pool: asyncpg.Pool, incident_id: str) -> asyncpg.Record:
    row = await pool.fetchrow("SELECT * FROM incidents WHERE id = $1", uuid.UUID(incident_id))
    assert row is not None
    return row


async def test_a_proposed_rollback_is_stored_with_its_outcome(pool: asyncpg.Pool) -> None:
    await _deploy_row(pool)
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _stored_deps(pool))

    assert result.status == "rollback_proposed"
    row = await _incident_row(pool, request.incident_id)
    assert row["status"] == "rollback_proposed"
    assert row["resolution_type"] == "rollback"
    assert row["root_cause"] == "pr-42 changed GET / to return HTML"
    assert row["root_cause_confidence"] == pytest.approx(0.9)
    assert row["resolution"] == "the previous version passed the same checks"
    assert row["reflexion_loops"] == 0
    assert row["langfuse_trace_id"] == uuid.UUID(request.incident_id).hex
    assert row["correlation_id"] == uuid.UUID(request.correlation_id)
    assert row["service"] == SERVICE and row["severity"] == "P2"  # triage's severity
    trajectory = json.loads(row["trajectory"])
    assert PipelineResult.model_validate(trajectory["pipeline_result"]) == result
    assert trajectory["provenance"]["root_cause_by"] == "resolution"
    assert trajectory["provenance"]["resolution_by"] == "resolution"
    assert trajectory["provenance"]["model_generated"] is True
    actions = [e["action"] for e in trajectory["timeline"]["timeline"]]
    assert "prepare_rollback_spec" in actions and "reverification" in actions


async def test_an_escalation_is_stored_with_its_loops_and_reason(pool: asyncpg.Pool) -> None:
    request = _request()
    with fake_script() as script:
        script.on("triage", _triage())
        script.on("analysis", *_analysis(), *_analysis(), *_analysis())
        script.on(REFLEXION_AGENT, _low(), _low(), _low())
        result = await run_pipeline(request, _stored_deps(pool))

    assert result.status == "escalated"
    row = await _incident_row(pool, request.incident_id)
    assert row["status"] == "escalated" and row["resolution_type"] == "escalated"
    assert row["reflexion_loops"] == 2
    assert row["root_cause"] == "GET / returns HTML since pr-42"
    assert row["root_cause_confidence"] == pytest.approx(0.4)
    assert row["resolution"].startswith("escalated: decision gate: confidence 0.40")
    provenance = json.loads(row["trajectory"])["provenance"]
    assert provenance["root_cause_by"] == "analysis"
    assert provenance["resolution_by"] == orchestrator.ORCHESTRATOR


async def test_unavailable_memory_is_logged_and_the_result_still_returned(
    target_ok: list[TargetDeploy], monkeypatch: pytest.MonkeyPatch
) -> None:
    log = MagicMock()
    monkeypatch.setattr(orchestrator, "logger", log)
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, _deps())  # EpisodicMemory(None): no database

    assert result.status == "rollback_proposed"
    warning = log.warning.call_args
    assert warning.args == ("incident_not_stored",)
    assert warning.kwargs["error_type"] == "SentinelMemoryError"


async def test_persist_false_stores_nothing(target_ok: list[TargetDeploy]) -> None:
    episodic = MagicMock()
    episodic.store_incident = AsyncMock()
    deps = dataclasses.replace(_deps(), episodic_memory=episodic, persist=False)
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, deps)
    assert result.status == "rollback_proposed"
    episodic.store_incident.assert_not_awaited()


async def test_the_stored_outcome_mirrors_the_result(target_ok: list[TargetDeploy]) -> None:
    episodic = MagicMock()
    episodic.store_incident = AsyncMock()
    deps = dataclasses.replace(_deps(), episodic_memory=episodic)
    request = _request()
    with fake_script() as script:
        _happy(script, request)
        result = await run_pipeline(request, deps)

    episodic.store_incident.assert_awaited_once()
    (incident,), kwargs = episodic.store_incident.call_args
    outcome = kwargs["outcome"]
    assert incident.id == request.incident_id and incident.status == "rollback_proposed"
    assert outcome.resolution_type == result.resolution_type == "rollback"
    assert outcome.root_cause_confidence == result.confidence
    assert outcome.reflexion_loops == result.reflexion_loops
    assert outcome.pipeline_result == result.model_dump(mode="json")
