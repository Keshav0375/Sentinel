"""prepare_rollback_spec and format_escalation tools — output-only resolution drafts.

The backend reasons and drafts; GitHub Actions executes (backend.md §3.3, §4.8). These
two tools turn the agent's conclusion into a typed result and nothing more: no network,
no database, no clock, no logging — the output depends on the input alone.

- ``prepare_rollback_spec`` → :class:`RollbackSpec`, the §3.4 ``/generate/pr-content``
  body (decision 2026-10-14, "Backend phase 3 (tools) contract"). The incident workflow
  reverts ``target_deploy.commit_sha`` (its ``TARGET_SHA``) in a PR that a human merges
  or closes on GitHub.
- ``format_escalation`` → :class:`EscalationContext`, the hand-off to a human when no
  rollback is drafted.

Neither applies the rollback confidence threshold: whether a spec is acted on is the
orchestrator's decision. Unusable arguments return a ToolFailure (``bad_input``).
"""

from __future__ import annotations

import math
import re
from datetime import UTC

from agents import FunctionTool, function_tool

from sentinel.models.tools import (
    EscalationContext,
    EscalationInput,
    RollbackSpec,
    RollbackSpecInput,
    TargetDeploy,
    ToolFailure,
)
from sentinel.tools.output import tool_json

_ROLLBACK_TOOL = "prepare_rollback_spec"
_ESCALATION_TOOL = "format_escalation"

# A git commit id as the deploy pipeline records it: abbreviated (7+) up to the full 40.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def make_remediation_tools() -> list[FunctionTool]:
    """Create the two resolution tools. They take no dependencies: both are pure.

    Returns:
        ``[prepare_rollback_spec, format_escalation]``.
    """

    @function_tool(strict_mode=False)
    async def prepare_rollback_spec(request: RollbackSpecInput) -> str:
        """Draft the rollback of one deploy: the spec the incident workflow turns into a revert PR.

        Output only — this opens no PR and changes nothing. GitHub Actions creates the
        revert PR from the spec (reverting target_deploy.commit_sha), and a human merges
        or closes it on GitHub. Use it when one deploy is the cause; target_deploy is
        that deploy's record from get_deploy_details. Returns the spec as JSON; a
        failure ("error": true, kind "bad_input") names the argument to fix: commit_sha
        must be 7-40 hex characters, confidence between 0 and 1, and no text field may
        be empty.

        Args:
            request: The incident, root cause, evidence, confidence, justification and
                the deploy to revert.
        """
        return tool_json(_prepare_rollback_spec(request))

    @function_tool(strict_mode=False)
    async def format_escalation(request: EscalationInput) -> str:
        """Package the incident for a human when no rollback is drafted.

        Output only — this notifies no one. Gives the hypothesis, the evidence found,
        the confidence, and what is still missing (what a human should check first).
        Returns JSON; a failure ("error": true, kind "bad_input") names the argument
        to fix: the hypothesis must not be empty and confidence must be between 0 and 1.

        Args:
            request: Hypothesis, evidence items, confidence and missing items.
        """
        return tool_json(_format_escalation(request))

    return [prepare_rollback_spec, format_escalation]


def _prepare_rollback_spec(request: RollbackSpecInput) -> RollbackSpec | ToolFailure:
    """Validate the request and return the RollbackSpec, or a ToolFailure — never raise.

    Separated from the tool decorator so tests can call it without a ToolContext.
    """
    texts = {
        "incident_id": request.incident_id.strip(),
        "root_cause": request.root_cause.strip(),
        "evidence_summary": request.evidence_summary.strip(),
        "justification": request.justification.strip(),
    }
    for name, value in texts.items():
        if not value:
            return _failure(_ROLLBACK_TOOL, f"{name} must not be empty")
    if (problem := _confidence_problem(request.confidence)) is not None:
        return _failure(_ROLLBACK_TOOL, problem)

    deploy = request.target_deploy
    sha = deploy.commit_sha.strip()
    if not _SHA_RE.fullmatch(sha):
        return _failure(
            _ROLLBACK_TOOL,
            f"target_deploy.commit_sha must be 7-40 hex characters, got {deploy.commit_sha!r}",
        )
    if deploy.pr_number < 1:
        return _failure(
            _ROLLBACK_TOOL, f"target_deploy.pr_number must be positive, got {deploy.pr_number}"
        )
    if deploy.deployed_at.utcoffset() is None:
        return _failure(_ROLLBACK_TOOL, "target_deploy.deployed_at must carry a timezone")

    return RollbackSpec(
        incident_id=texts["incident_id"],
        root_cause=texts["root_cause"],
        evidence_summary=texts["evidence_summary"],
        confidence=request.confidence,
        justification=texts["justification"],
        target_deploy=TargetDeploy(
            pr_number=deploy.pr_number,
            commit_sha=sha.lower(),
            deployed_at=deploy.deployed_at.astimezone(UTC),
        ),
    )


def _format_escalation(request: EscalationInput) -> EscalationContext | ToolFailure:
    """Validate and tidy the request into an EscalationContext, or a ToolFailure.

    Items are stripped; blank and repeated items are dropped, first occurrence kept.
    """
    hypothesis = request.hypothesis.strip()
    if not hypothesis:
        return _failure(_ESCALATION_TOOL, "hypothesis must not be empty")
    if (problem := _confidence_problem(request.confidence)) is not None:
        return _failure(_ESCALATION_TOOL, problem)
    return EscalationContext(
        hypothesis=hypothesis,
        evidence=_items(request.evidence),
        confidence=request.confidence,
        missing=_items(request.missing),
    )


def _confidence_problem(confidence: float) -> str | None:
    if math.isfinite(confidence) and 0.0 <= confidence <= 1.0:
        return None
    return f"confidence must be between 0 and 1, got {confidence}"


def _items(values: list[str]) -> list[str]:
    stripped = (value.strip() for value in values)
    return list(dict.fromkeys(value for value in stripped if value))


def _failure(tool: str, message: str) -> ToolFailure:
    return ToolFailure(tool=tool, kind="bad_input", message=message)
