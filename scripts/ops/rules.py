#!/usr/bin/env python3
"""Derive ops.v1 events from Sentinel workflow facts (backend task 10.2; ops-dashboard §4).

The single shared derivation for the Sentinel workflows: emit.py calls it at event time from the
workflow's own `needs.*.result` values, and the reconciler (reconcile.py, task 10.3) calls it with
the jobs API's results and times. Both hash the same kind/source/source_id/state, so a backfill
lands on the event_id the workflow already wrote (§3). Pure functions, stdlib plus `ledger`.

Who emits what (source is `gha:Keshav0375/Sentinel/<file>`):

  workflow                 job          kinds
  ci_backend_deployment    ops-start    build.started
  ci_backend_deployment    ops-events   build.succeeded|failed, deploy.succeeded|failed
  ci_incident_response     ops-start    alert.received, agent.started
  ci_incident_response     ops-events   agent.decided|failed, escalation.raised
  ci_backend_scale         ops-events   scale.changed
  infra_runners            ops-start    build.started
  infra_runners            ops-events   build.succeeded|failed
  infra_deploy (apply)     ops-start    deploy.started
  infra_deploy (apply)     ops-events   deploy.succeeded|failed

Backend deploy (Q run-quality, B build-and-push, D deploy-to-aks, T run-tests; bad = failure or
cancelled): Q missing or skipped -> nothing. Build: Q bad -> build.failed "quality"; B success ->
build.succeeded; B bad -> build.failed "build". Deploy (B success only): D bad -> deploy.failed
"deploy"; D and T success -> deploy.succeeded; D success, T bad -> deploy.failed "test".
deploy.started is reconciler-only (with_started).

Incident: alert.received (state = signal_type) and agent.started at the start; at the end the
pipeline's success gives agent.decided (state = resolution_type) plus escalation.raised when
escalated, else a bad ensure-backend-up or pipeline gives agent.failed. Free text that looks like a
secret is dropped, never the event.

Scale (scale job success only): nightly -> 0 replicas; manual up -> BACKEND_REPLICAS; manual down
-> 0 unless keep_warm (true or unknown -> nothing changed, no event).

Infra: runner build -> build.*; apply -> deploy.* ("guards", "apply" or "verify" on failure). The
chain is `{run_id}` only so an infra run never merges into a backend chain on the same commit.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import ledger

OWNER = "Keshav0375"
SENTINEL = "Keshav0375/Sentinel"
DEPLOYMENT = "Keshav0375/Sentinel-deployment"
BACKEND_DEPLOY_WF = "ci_backend_deployment.yml"
INCIDENT_WF = "ci_incident_response.yml"
SCALE_WF = "ci_backend_scale.yml"
INFRA_RUNNERS_WF = "infra_runners.yml"
INFRA_DEPLOY_WF = "infra_deploy.yml"
# azure/k8s/deployment.yaml `replicas` (a test pins it).
BACKEND_REPLICAS = 1

BAD = frozenset({"failure", "cancelled"})
# incident_dispatch.SIGNAL_TYPES (a test keeps them equal; rules stays free of that import).
SIGNAL_TYPES = ("deploy_failure", "runtime_error")
SCENARIO_RE = re.compile(r"^(pass|deployfail|runtime)/(0[1-9]|10)$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SEVERITY_RE = re.compile(r"^[A-Za-z0-9 _-]{1,32}$")

# Job display name -> job key (the jobs API reports names).
BACKEND_DEPLOY_JOBS = {
    "Check Azure CI identity": "preflight",
    "Run Quality Checks": "run-quality",
    "Build and Push Image": "build-and-push",
    "Deploy to AKS": "deploy-to-aks",
    "Run Tests Against Live Deployment": "run-tests",
    "Promote or Rollback": "promote-or-rollback",
}
SCALE_STEPS = {
    "up": "Bring the backend up",
    "down-manual": "Scale the backend down (manual, honours SENTINEL_KEEP_WARM)",
    "down-nightly": "Scale the backend to zero (nightly safety net, ignores SENTINEL_KEEP_WARM)",
}
INFRA_RUNNERS_JOBS = {
    "Check infra OIDC readiness": "oidc-ready",
    "Build and Push Runner Image": "build-and-push",
}
INFRA_DEPLOY_VERIFY_JOB = "Verify against Azure"
# The run job's name is `${{ inputs.action }} · ${{ inputs.scope }}`.
INFRA_DEPLOY_RUN_JOB_PREFIX = "apply · "
INFRA_GUARDS_STEP = "Check guards"


@dataclass(frozen=True)
class EventSpec:
    kind: str
    source: str
    source_id: str
    state: str = ""
    chain: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    ts: str | None = None

    @property
    def event_id(self) -> str:
        return ledger.compute_event_id(self.kind, self.source, self.source_id, self.state)

    def build(
        self,
        *,
        recorded_at: datetime | None = None,
        warn: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """The validated ops.v1 event (raises ledger.LedgerError)."""
        event: dict[str, Any] = ledger.build_event(
            kind=self.kind,
            source=self.source,
            source_id=self.source_id,
            state=self.state,
            ts=self.ts,
            chain=self.chain,
            data=self.data,
            recorded_at=recorded_at,
            warn=warn,
        )
        return event


@dataclass(frozen=True)
class JobFact:
    result: str = ""
    started_at: str | None = None
    completed_at: str | None = None


# ── Shared recipes ────────────────────────────────────────────────────────────


def gha_source(repo: str, workflow_file: str) -> str:
    return f"gha:{repo}/{workflow_file}"


def run_source_id(run_id: int, run_attempt: int) -> str:
    return f"{run_id}.{run_attempt}"


def run_url(repo: str, run_id: int) -> str:
    return f"https://github.com/{repo}/actions/runs/{run_id}"


def _parse_ts(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def seconds_between(start: str | None, end: str | None) -> float | None:
    """Whole seconds from *start* to *end* (RFC 3339); None if either is missing or end < start."""
    if not start or not end:
        return None
    begin, finish = _parse_ts(start), _parse_ts(end)
    if begin is None or finish is None or finish < begin:
        return None
    return float(round((finish - begin).total_seconds()))


def parse_bool(value: str | None) -> bool | None:
    """A workflow boolean: "true" -> True, anything else -> False, None (unset) -> None."""
    if value is None:
        return None
    return value.strip().lower() == "true"


def _run_data(sha: str, url: str, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"version": f"sha-{sha[:7]}", "run_url": url}
    data.update({key: value for key, value in extra.items() if value is not None})
    return data


def _run_event(
    kind: str,
    workflow: str,
    *,
    sha: str,
    run_id: int,
    run_attempt: int,
    chain: dict[str, Any],
    ts: str | None = None,
    **extra: Any,
) -> EventSpec:
    return EventSpec(
        kind=kind,
        source=gha_source(SENTINEL, workflow),
        source_id=run_source_id(run_id, run_attempt),
        chain=chain,
        data=_run_data(sha, run_url(SENTINEL, run_id), **extra),
        ts=ts,
    )


# ── Backend deployment ────────────────────────────────────────────────────────


def backend_build_started(
    *, sha: str, run_id: int, run_attempt: int, ts: str | None = None
) -> EventSpec:
    return _run_event(
        "build.started",
        BACKEND_DEPLOY_WF,
        sha=sha,
        run_id=run_id,
        run_attempt=run_attempt,
        chain={"sha": sha, "run_id": run_id},
        ts=ts,
    )


def backend_deploy_events(
    jobs: Mapping[str, JobFact],
    *,
    sha: str,
    run_id: int,
    run_attempt: int,
    with_started: bool = False,
) -> list[EventSpec]:
    """build.*/deploy.* for one backend deployment run (keys are job ids)."""
    quality = jobs.get("run-quality")
    if quality is None or quality.result == "skipped":
        return []
    build = jobs.get("build-and-push", JobFact())
    deploy = jobs.get("deploy-to-aks", JobFact())
    tests = jobs.get("run-tests", JobFact())
    chain = {"sha": sha, "run_id": run_id}

    def event(kind: str, ts: str | None = None, **extra: Any) -> EventSpec:
        return _run_event(
            kind,
            BACKEND_DEPLOY_WF,
            sha=sha,
            run_id=run_id,
            run_attempt=run_attempt,
            chain=chain,
            ts=ts,
            **extra,
        )

    events: list[EventSpec] = []
    if with_started and (quality.started_at or quality.result):
        events.append(event("build.started", quality.started_at))

    def build_terminal(kind: str, decider: JobFact, stage: str | None = None) -> EventSpec:
        return event(
            kind,
            decider.completed_at,
            duration_s=seconds_between(quality.started_at, decider.completed_at),
            failed_stage=stage,
        )

    if quality.result in BAD:
        events.append(build_terminal("build.failed", quality, "quality"))
    elif build.result == "success":
        events.append(build_terminal("build.succeeded", build))
    elif build.result in BAD:
        events.append(build_terminal("build.failed", build, "build"))

    if build.result != "success":
        return events
    if with_started and deploy.started_at and deploy.result != "skipped":
        events.append(event("deploy.started", deploy.started_at))

    def deploy_terminal(kind: str, decider: JobFact, stage: str | None = None) -> EventSpec:
        return event(
            kind,
            decider.completed_at,
            duration_s=seconds_between(deploy.started_at, decider.completed_at),
            failed_stage=stage,
        )

    if deploy.result in BAD:
        events.append(deploy_terminal("deploy.failed", deploy, "deploy"))
    elif deploy.result == "success" and tests.result == "success":
        events.append(deploy_terminal("deploy.succeeded", tests))
    elif deploy.result == "success" and tests.result == "skipped":
        # Deployed, tests skipped: a terminal outcome (v1 has no field for "tests skipped"), so the
        # chain never stays running or goes stale.
        events.append(deploy_terminal("deploy.succeeded", deploy))
    elif deploy.result == "success" and tests.result in BAD:
        events.append(deploy_terminal("deploy.failed", tests, "test"))
    return events


# ── Incident response ─────────────────────────────────────────────────────────


def _incident_event(
    kind: str, correlation_id: str, chain: dict[str, Any], data: dict[str, Any], state: str = ""
) -> EventSpec:
    return EventSpec(
        kind=kind,
        source=gha_source(SENTINEL, INCIDENT_WF),
        source_id=correlation_id,
        state=state,
        chain=chain,
        data=data,
    )


def incident_start_events(
    *,
    correlation_id: str,
    signal_type: str | None,
    service: str | None,
    severity: str | None,
    run_id: int,
) -> list[EventSpec]:
    """alert.received (only for a known signal type) and agent.started."""
    chain = {"correlation_id": correlation_id}
    events: list[EventSpec] = []
    if signal_type in SIGNAL_TYPES:
        data: dict[str, Any] = {
            "signal_type": signal_type,
            "service": service if service and SERVICE_RE.match(service) else "unknown",
        }
        if severity and SEVERITY_RE.match(severity):
            data["severity"] = severity
        events.append(
            _incident_event("alert.received", correlation_id, chain, data, state=signal_type)
        )
    events.append(
        _incident_event(
            "agent.started", correlation_id, chain, {"run_url": run_url(SENTINEL, run_id)}
        )
    )
    return events


def _confidence(value: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return min(max(number, 0.0), 1.0)


def _safe_text(kind: str, name: str, text: str, warn: Callable[[str], None] | None) -> str | None:
    """*text* when non-empty and secret-free; a secret-like value drops the field, not the event."""
    if not text:
        return None
    if ledger.find_secrets({"t": text}):
        if warn is not None:
            warn(f"{kind}: {name} dropped (secret-like value)")
        return None
    return text


def incident_result_events(
    *,
    correlation_id: str,
    ensure_result: str,
    pipeline_result: str,
    resolution_type: str,
    confidence: str,
    root_cause: str,
    evidence: str,
    incident_id: str,
    pr_details: Mapping[str, Any] | None,
    run_id: int,
    warn: Callable[[str], None] | None = None,
) -> list[EventSpec]:
    """agent.decided (+ escalation.raised) or agent.failed for one incident run."""
    chain: dict[str, Any] = {"correlation_id": correlation_id}
    if UUID_RE.match(incident_id or ""):
        chain["incident_id"] = incident_id
    if isinstance(pr_details, Mapping):
        sha = pr_details.get("sha")
        if isinstance(sha, str) and len(sha) == 40 and SHA_RE.match(sha):
            chain["sha"] = sha
        branch = pr_details.get("branch")
        if isinstance(branch, str) and SCENARIO_RE.match(branch):
            chain["scenario"] = branch
    url = run_url(SENTINEL, run_id)

    if pipeline_result == "success":
        resolution = (
            resolution_type if resolution_type in ("rollback", "escalated") else "escalated"
        )
        decided: dict[str, Any] = {
            "resolution_type": resolution,
            "confidence": _confidence(confidence),
            "root_cause_summary": _safe_text(
                "agent.decided", "root_cause_summary", root_cause, warn
            ),
            "run_url": url,
        }
        events = [
            _incident_event(
                "agent.decided",
                correlation_id,
                chain,
                {key: value for key, value in decided.items() if value is not None},
                state=resolution,
            )
        ]
        if resolution == "escalated":
            summary = _safe_text("escalation.raised", "summary", root_cause or evidence, warn)
            data: dict[str, Any] = {"run_url": url}
            if summary is not None:
                data = {"summary": summary, "run_url": url}
            events.append(_incident_event("escalation.raised", correlation_id, chain, data))
        return events
    if ensure_result in BAD:
        error_class = "cancelled" if ensure_result == "cancelled" else "backend_unavailable"
    elif pipeline_result in BAD:
        error_class = "cancelled" if pipeline_result == "cancelled" else "pipeline_fault"
    else:
        return []
    return [
        _incident_event(
            "agent.failed", correlation_id, chain, {"error_class": error_class, "run_url": url}
        )
    ]


# ── Scale ─────────────────────────────────────────────────────────────────────


def scale_events(
    *,
    event_name: str,
    direction: str,
    result: str,
    keep_warm: bool | None,
    run_id: int,
    run_attempt: int,
    ts: str | None = None,
) -> list[EventSpec]:
    """scale.changed when the scale job succeeded and the replica count changed."""
    if result != "success":
        return []
    if event_name == "schedule":
        replicas = 0
    elif event_name == "workflow_dispatch" and direction == "up":
        replicas = BACKEND_REPLICAS
    elif event_name == "workflow_dispatch" and direction == "down" and keep_warm is False:
        replicas = 0
    else:
        return []
    data: dict[str, Any] = {"replicas": replicas, "run_url": run_url(SENTINEL, run_id)}
    if keep_warm is not None:
        data["keep_warm"] = keep_warm
    return [
        EventSpec(
            kind="scale.changed",
            source=gha_source(SENTINEL, SCALE_WF),
            source_id=run_source_id(run_id, run_attempt),
            state=str(replicas),
            chain={"run_id": run_id},
            data=data,
            ts=ts,
        )
    ]


# ── Infra ─────────────────────────────────────────────────────────────────────


def _infra_event(
    kind: str,
    workflow: str,
    *,
    sha: str,
    run_id: int,
    run_attempt: int,
    ts: str | None = None,
    **extra: Any,
) -> EventSpec:
    return _run_event(
        kind,
        workflow,
        sha=sha,
        run_id=run_id,
        run_attempt=run_attempt,
        chain={"run_id": run_id},
        ts=ts,
        **extra,
    )


def infra_build_started(
    *, sha: str, run_id: int, run_attempt: int, ts: str | None = None
) -> EventSpec:
    return _infra_event(
        "build.started", INFRA_RUNNERS_WF, sha=sha, run_id=run_id, run_attempt=run_attempt, ts=ts
    )


def infra_build_events(
    build: JobFact, *, sha: str, run_id: int, run_attempt: int, with_started: bool = False
) -> list[EventSpec]:
    """build.* for one runner image build (build-and-push job)."""
    if build.result == "skipped":
        return []
    events: list[EventSpec] = []
    if with_started and (build.started_at or build.result):
        events.append(
            infra_build_started(
                sha=sha, run_id=run_id, run_attempt=run_attempt, ts=build.started_at
            )
        )
    if build.result == "success":
        kind, stage = "build.succeeded", None
    elif build.result in BAD:
        kind, stage = "build.failed", "build"
    else:
        return events
    events.append(
        _infra_event(
            kind,
            INFRA_RUNNERS_WF,
            sha=sha,
            run_id=run_id,
            run_attempt=run_attempt,
            ts=build.completed_at,
            duration_s=seconds_between(build.started_at, build.completed_at),
            failed_stage=stage,
        )
    )
    return events


def infra_deploy_started(
    *, sha: str, run_id: int, run_attempt: int, ts: str | None = None
) -> EventSpec:
    return _infra_event(
        "deploy.started", INFRA_DEPLOY_WF, sha=sha, run_id=run_id, run_attempt=run_attempt, ts=ts
    )


def infra_deploy_events(
    run: JobFact,
    verify: JobFact,
    *,
    guards_passed: bool,
    sha: str,
    run_id: int,
    run_attempt: int,
    with_started: bool = False,
) -> list[EventSpec]:
    """deploy.* for one infra apply (run and verify jobs); plan and destroy emit nothing."""
    events: list[EventSpec] = []
    if with_started and run.started_at and run.result != "skipped":
        events.append(
            infra_deploy_started(sha=sha, run_id=run_id, run_attempt=run_attempt, ts=run.started_at)
        )
    if run.result in BAD:
        kind, decider, stage = "deploy.failed", run, ("apply" if guards_passed else "guards")
    elif run.result == "success" and verify.result == "success":
        kind, decider, stage = "deploy.succeeded", verify, None
    elif run.result == "success" and verify.result in BAD:
        kind, decider, stage = "deploy.failed", verify, "verify"
    else:
        return events
    events.append(
        _infra_event(
            kind,
            INFRA_DEPLOY_WF,
            sha=sha,
            run_id=run_id,
            run_attempt=run_attempt,
            ts=decider.completed_at,
            duration_s=seconds_between(run.started_at, decider.completed_at),
            failed_stage=stage,
        )
    )
    return events
