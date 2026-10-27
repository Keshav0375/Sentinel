#!/usr/bin/env python3
"""Build the ops dashboard snapshot from a clone of the ops ledger (backend task 10.3; ops §6).

The snapshot is derived, never committed: the reconcile workflow rebuilds it on every run and
uploads it as the `ops-snapshot` artifact for Pages (10.5); the SPA (10.4) reads it. Every file is
valid against the frozen `schema/snapshot.v1.json` (dispatched on `file`):

  meta.json                 generated_at, ledger head, counts, source statuses
  chains.json               one chain_summary per chain, newest started_at first
  metrics.json              KPI windows (24h/7d/30d/all), current lane counts, daily, eval
  entities/<chain_id>.json  the chain drawer: its events, agent decision, trace, evals, links

Events are loaded from `events/**/*.json`, validated against event.v1 (an invalid file is skipped
with a warning), deduplicated by event_id and sorted by (ts, recorded_at, event_id). Chains are the
connected components over the chain keys correlation_id, incident_id, sha (first 7), pr and run_id;
chain_id = "c_" + sha256("<key>:<value>")[:16] of the first present of correlation_id, sha, pr,
run_id (value from the earliest event that has it), else of "event:<first event_id>".
incidents and MTTR are per correlation_id (an incident), not per chain.

CLI:
  snapshot.py --ledger DIR --out DIR [--status PATH] [--run-url URL] [--now RFC3339]
--status is the reconciler's status file, or a previous snapshot's meta.json (its `sources` are
carried over: a Pages rebuild pulls no source, so it reports the last reconcile's statuses).
Exit codes: 0 written, 2 a document failed validation (nothing written) or bad arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ops/: ledger

import ledger  # noqa: E402

SNAPSHOT_SCHEMA = "ops.snapshot.v1"
SENTINEL = "Keshav0375/Sentinel"
DEPLOYMENT = "Keshav0375/Sentinel-deployment"
BACKEND_SOURCE = f"gha:{SENTINEL}/ci_backend_deployment.yml"
APP_SOURCE = f"gha:{DEPLOYMENT}/ci_app_deployment.yml"
INFRA_RUNNERS_SOURCE = f"gha:{SENTINEL}/infra_runners.yml"
INFRA_DEPLOY_SOURCE = f"gha:{SENTINEL}/infra_deploy.yml"
SCALE_SOURCE = f"gha:{SENTINEL}/ci_backend_scale.yml"
STALE_RUNNING_S = 21600
MAX_ENTITY_EVENTS = 1000
MAX_EVALS = 100
MAX_LINKS = 50
MAX_DAILY = 90
MAX_SCENARIOS = 30
LABEL_MAX = 80
TITLE_MAX = 120
DEFAULT_SOURCES = {"github": "ok", "langfuse": "skipped"}
SOURCE_STATUSES = ("ok", "error", "skipped")
CHAIN_KEYS = ("correlation_id", "incident_id", "sha", "pr", "run_id")
CHAIN_ID_KEYS = ("correlation_id", "sha", "pr", "run_id")
WINDOWS: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}
RUN_LINK_LABELS = {
    "ci_backend_deployment.yml": "Backend deployment run",
    "ci_app_deployment.yml": "Deployment run",
    "ci_incident_response.yml": "Incident response run",
}
CHECK_NAMES = ("fired", "classified", "correlated", "revert_target")
JUDGE_NAMES = (
    "triage_accuracy",
    "root_cause_correctness",
    "tool_efficiency",
    "mttr",
    "remediation_safety",
)
BUILD_DEPLOY_TERMINALS = ("build.succeeded", "build.failed", "deploy.succeeded", "deploy.failed")
FAILURES = ("build.failed", "deploy.failed")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

Warn = Callable[[str], None]
Event = dict[str, Any]


def warn(message: str) -> None:
    print(f"::warning::ops-snapshot: {message}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ts(value: str) -> datetime:
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=UTC)


def _fmt(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_TS_FORMAT)


def _secs(start: str, end: str) -> float:
    return float(max((_ts(end) - _ts(start)).total_seconds(), 0))


def _data(event: Mapping[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _chain(event: Mapping[str, Any]) -> dict[str, Any]:
    chain = event.get("chain")
    return chain if isinstance(chain, dict) else {}


def _of(events: Iterable[Event], *kinds: str) -> list[Event]:
    return [e for e in events if e["kind"] in kinds]


def _last(events: Iterable[Event], *kinds: str) -> Event | None:
    found = _of(events, *kinds)
    return found[-1] if found else None


def _first(events: Iterable[Event], *kinds: str) -> Event | None:
    found = _of(events, *kinds)
    return found[0] if found else None


def _cut(text: str, limit: int) -> str:
    return text[:limit]


def _flagged(event: Mapping[str, Any]) -> bool:
    data = _data(event)
    return bool(data.get("judge_failed")) or bool(data.get("judge_conflict"))


def _score(event: Mapping[str, Any]) -> float | None:
    """total_score of an eval.scored event, None when a judge flag is set or it is absent."""
    if _flagged(event):
        return None
    value = _data(event).get("total_score")
    return float(value) if isinstance(value, int | float) else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def nearest_rank(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(q * len(ordered)) - 1, 0)]


def _source_file(event: Mapping[str, Any]) -> str:
    source = str(event.get("source", ""))
    return source.rsplit("/", 1)[-1] if source.startswith("gha:") else ""


# ── Loading and grouping ──────────────────────────────────────────────────────


def load_events(ledger_dir: Path, warn: Warn = warn) -> list[Event]:
    """Every valid event under events/, deduplicated by event_id, in (ts, recorded_at, id) order."""
    root = ledger_dir / "events"
    if not root.is_dir():
        return []
    schema = ledger.load_schema(ledger.EVENT_SCHEMA_PATH)
    by_id: dict[str, Event] = {}
    for path in sorted(root.rglob("*.json")):
        rel = path.relative_to(ledger_dir).as_posix()
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            warn(f"{rel}: not a JSON document, skipped")
            continue
        errors = ledger.validate(event, schema)
        if errors:
            warn(f"{rel}: {errors[0]}, skipped")
            continue
        known = by_id.get(event["event_id"])
        if known is None or event["recorded_at"] < known["recorded_at"]:
            by_id[event["event_id"]] = event
    return sorted(by_id.values(), key=lambda e: (e["ts"], e["recorded_at"], e["event_id"]))


def _keys(event: Mapping[str, Any]) -> list[tuple[str, Any]]:
    chain = _chain(event)
    keys: list[tuple[str, Any]] = []
    for name in CHAIN_KEYS:
        if name in chain:
            value = chain[name]
            keys.append((name, value[:7] if name == "sha" else value))
    return keys


def group_chains(events: list[Event]) -> list[list[Event]]:
    """Connected components over the chain keys; an event with no key is its own chain."""
    parent = list(range(len(events)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[tuple[str, Any], int] = {}
    for i, event in enumerate(events):
        for key in _keys(event):
            if key in owner:
                a, b = find(i), find(owner[key])
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                owner[key] = i
    groups: dict[int, list[Event]] = {}
    for i, event in enumerate(events):
        groups.setdefault(find(i), []).append(event)
    return list(groups.values())


def chain_id(events: list[Event]) -> str:
    for name in CHAIN_ID_KEYS:
        for event in events:
            if name in _chain(event):
                text = f"{name}:{_chain(event)[name]}"
                return "c_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    text = f"event:{events[0]['event_id']}"
    return "c_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ── Status and stages ─────────────────────────────────────────────────────────


def _open_stage(events: list[Event]) -> bool:
    for family in ("build", "deploy"):
        # Paired per run_id on time: a re-run (same run_id, new attempt) is open until its own
        # terminal, so the latest start must not be after the latest terminal.
        started: dict[Any, tuple[str, str]] = {}
        closed: dict[Any, tuple[str, str]] = {}
        for event in _of(events, f"{family}.started"):
            run, at = _chain(event).get("run_id"), (event["ts"], event["recorded_at"])
            started[run] = max(started.get(run, at), at)
        for event in _of(events, f"{family}.succeeded", f"{family}.failed"):
            run, at = _chain(event).get("run_id"), (event["ts"], event["recorded_at"])
            closed[run] = max(closed.get(run, at), at)
        for run, at in started.items():
            if run not in closed or at > closed[run]:
                return True
    ended = bool(_of(events, "agent.decided", "agent.failed"))
    if _of(events, "alert.received") and not ended:
        return True
    if _of(events, "agent.started") and not ended:
        return True
    rollback = any(
        _data(e).get("resolution_type") == "rollback" for e in _of(events, "agent.decided")
    )
    return rollback and not _of(events, "revert_pr.opened")


def chain_status(events: list[Event], now: datetime) -> str:
    kinds = {e["kind"] for e in events}
    if "revert_pr.merged" in kinds:
        return "reverted"
    if "revert_pr.closed" in kinds:
        return "closed"
    if "revert_pr.opened" in kinds:
        return "awaiting_review"
    if "escalation.raised" in kinds:
        return "escalated"
    # The latest agent outcome wins: a re-run that decided after an earlier agent.failed is judged
    # by its decision (a rollback with no PR yet stays running below).
    agent = _last(events, "agent.decided", "agent.failed")
    if agent is not None and agent["kind"] == "agent.failed":
        return "failed"
    if _open_stage(events):
        stale = (now - _ts(events[-1]["ts"])).total_seconds() > STALE_RUNNING_S
        return "failed" if stale else "running"
    terminal = _last(events, *BUILD_DEPLOY_TERMINALS)
    if terminal is not None and terminal["kind"] in FAILURES:
        return "failed"
    gate = _last(events, "ci.pr_gate.completed")
    if gate is not None and _data(gate).get("conclusion") != "success":
        return "failed"
    return "succeeded"


def _stage(
    name: str,
    status: str,
    label: str,
    event: Mapping[str, Any] | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    data = _data(event) if event is not None else {}
    duration = data.get("duration_s")
    return {
        "name": name,
        "status": status,
        "at": event["ts"] if event is not None else None,
        "duration_s": duration if isinstance(duration, int | float) else None,
        "label": _cut(label, LABEL_MAX),
        "url": url,
    }


def _with(prefix: str, value: Any) -> str:
    return f"{prefix} {value}" if value not in (None, "") else prefix


def _deploy_stage(events: list[Event]) -> dict[str, Any]:
    latest = _last(
        events,
        "build.started",
        "build.succeeded",
        "build.failed",
        "deploy.started",
        "deploy.succeeded",
        "deploy.failed",
    )
    if latest is not None:
        data, kind = _data(latest), latest["kind"]
        url = data.get("run_url")
        version = data.get("version")
        if kind == "deploy.succeeded":
            return _stage("deploy", "succeeded", _with("Deployed", version), latest, url)
        if kind == "deploy.failed":
            stage = data.get("failed_stage")
            label = _with("Failed at", stage) if stage else "Deploy failed"
            return _stage("deploy", "failed", label, latest, url)
        if kind == "build.failed":
            return _stage("deploy", "failed", "Build failed", latest, url)
        if kind == "deploy.started":
            return _stage("deploy", "running", _with("Deploying", version), latest, url)
        return _stage("deploy", "running", _with("Building", version), latest, url)
    gate = _last(events, "ci.pr_gate.completed")
    if gate is not None:
        conclusion = _data(gate).get("conclusion")
        status = "succeeded" if conclusion == "success" else "failed"
        url = _data(gate).get("run_url")
        return _stage("deploy", status, _with("PR gate", conclusion), gate, url)
    scale = _last(events, "scale.changed")
    if scale is not None:
        label = _with("Scaled to", _data(scale).get("replicas"))
        return _stage("deploy", "succeeded", label, scale, _data(scale).get("run_url"))
    return _stage("deploy", "none", "No deploy")


def _alert_stage(events: list[Event]) -> dict[str, Any]:
    alert = _last(events, "alert.received")
    if alert is None:
        return _stage("alert", "none", "No alert")
    data = _data(alert)
    label = str(data.get("signal_type") or "Alert")
    if data.get("severity"):
        label = f"{label}, {data['severity']}"
    return _stage("alert", "succeeded", label, alert, data.get("monitor_url"))


def _agent_stage(events: list[Event]) -> dict[str, Any]:
    decided = _last(events, "agent.decided")
    if decided is not None:
        data = _data(decided)
        resolution = str(data.get("resolution_type") or "")
        label = f"{resolution.capitalize()}, confidence {float(data.get('confidence') or 0):.2f}"
        return _stage("agent", "succeeded", label, decided, data.get("run_url"))
    failed = _last(events, "agent.failed")
    if failed is not None:
        return _stage("agent", "failed", "Agent failed", failed, _data(failed).get("run_url"))
    started = _last(events, "agent.started")
    if started is not None:
        return _stage("agent", "running", "Agent running", started, _data(started).get("run_url"))
    return _stage("agent", "none", "No agent run")


def _pr_number(events: list[Event]) -> int | None:
    for event in events:
        if event["kind"].startswith("revert_pr.") and "pr" in _chain(event):
            return int(_chain(event)["pr"])
    return None


def _pr_url(events: list[Event]) -> str | None:
    event = _first(
        events,
        "revert_pr.opened",
        "revert_pr.review_submitted",
        "revert_pr.merged",
        "revert_pr.closed",
    )
    return _data(event).get("url") if event is not None else None


def stages(events: list[Event]) -> list[dict[str, Any]]:
    out = [_deploy_stage(events), _alert_stage(events), _agent_stage(events)]
    escalation = _last(events, "escalation.raised")
    if escalation is not None:
        out.append(
            _stage(
                "escalation",
                "waiting",
                "Escalated to a human",
                escalation,
                _data(escalation).get("run_url"),
            )
        )
        return out
    opened = _last(events, "revert_pr.opened")
    merged = _last(events, "revert_pr.merged")
    closed = _last(events, "revert_pr.closed")
    url = _pr_url(events)
    number = _pr_number(events)
    if opened is not None:
        out.append(_stage("pr", "succeeded", f"Revert PR #{number} opened", opened, url))
    else:
        out.append(_stage("pr", "none", "No revert PR"))
    review = _last(events, "revert_pr.review_submitted")
    if review is not None:
        data = _data(review)
        login = data.get("reviewer")
        state = data.get("review_state")
        if state == "APPROVED":
            out.append(_stage("review", "succeeded", f"Approved by {login}", review, url))
        elif state == "CHANGES_REQUESTED":
            out.append(_stage("review", "failed", f"Changes requested by {login}", review, url))
        else:
            out.append(_stage("review", "waiting", f"Commented by {login}", review, url))
    elif opened is not None and merged is None and closed is None:
        out.append(_stage("review", "waiting", "Awaiting review", None, url))
    else:
        out.append(_stage("review", "none", "No review"))
    if merged is not None:
        out.append(_stage("merge", "succeeded", "Merged", merged, url))
    elif closed is not None:
        out.append(_stage("merge", "skipped", "Closed without merge", closed, url))
    else:
        out.append(_stage("merge", "none", "Not merged"))
    return out


# ── Chain summary ─────────────────────────────────────────────────────────────


def _needs_action(events: list[Event], status: str) -> dict[str, Any] | None:
    if status == "awaiting_review":
        opened = _last(events, "revert_pr.opened")
        if opened is not None:
            return {
                "reason": "pr_awaiting_review",
                "since": opened["ts"],
                "url": _data(opened).get("url"),
            }
    if status == "escalated":
        escalation = _last(events, "escalation.raised")
        if escalation is not None:
            return {
                "reason": "escalation",
                "since": escalation["ts"],
                "url": _data(escalation).get("run_url"),
            }
    if status == "failed" and not _of(events, "alert.received"):
        failure = _last(events, *FAILURES)
        if failure is not None:
            return {
                "reason": "failed_no_incident",
                "since": failure["ts"],
                "url": _data(failure).get("run_url"),
            }
    return None


def _earliest(events: list[Event], key: str) -> Any:
    for event in events:
        if key in _chain(event):
            return _chain(event)[key]
    return None


def _version(events: list[Event]) -> Any:
    for event in reversed(events):
        if _data(event).get("version"):
            return _data(event)["version"]
    return None


def _failed_suffix(events: list[Event]) -> str:
    terminal = _last(events, *BUILD_DEPLOY_TERMINALS)
    if terminal is not None and terminal["kind"] in FAILURES:
        stage = _data(terminal).get("failed_stage")
        return f" failed at {stage}" if stage else " failed"
    return ""


def _title(events: list[Event], status: str, cid: str, scenario: str | None) -> str:
    number = _pr_number(events)
    by_source: dict[str, list[Event]] = {}
    for event in events:
        by_source.setdefault(str(event["source"]), []).append(event)
    if status == "reverted":
        body = f"reverted by PR #{number}"
    elif status == "closed":
        body = f"revert PR #{number} closed"
    elif status == "awaiting_review":
        body = f"revert PR #{number} awaiting review"
    elif status == "escalated":
        body = "escalated to a human"
    elif BACKEND_SOURCE in by_source:
        body = _with("Backend deploy", _version(by_source[BACKEND_SOURCE]))
    elif APP_SOURCE in by_source:
        own = by_source[APP_SOURCE]
        body = _with("App deploy", _version(own)) + _failed_suffix(own)
    elif INFRA_RUNNERS_SOURCE in by_source:
        body = _with("Runner image", _version(by_source[INFRA_RUNNERS_SOURCE]))
    elif INFRA_DEPLOY_SOURCE in by_source:
        own = by_source[INFRA_DEPLOY_SOURCE]
        body = _with("Infra apply", _version(own)) + _failed_suffix(own)
    elif _of(events, "ci.pr_gate.completed"):
        sha = _earliest(events, "sha")
        body = _with("PR gate", sha[:7] if isinstance(sha, str) else None)
    elif _of(events, "scale.changed"):
        body = _with("Backend scaled to", _data(_of(events, "scale.changed")[-1]).get("replicas"))
    elif _of(events, "trace.summarized"):
        trace_id = str(_data(_of(events, "trace.summarized")[-1]).get("trace_id", ""))
        body = f"Agent trace {trace_id[:12]}"
    else:
        body = f"Chain {cid}"
    return _cut(f"{scenario}: {body}" if scenario else body, TITLE_MAX)


def _repo(events: list[Event]) -> str:
    for event in events:
        if str(event["source"]).startswith(f"gha:{DEPLOYMENT}/") or event["kind"].startswith(
            "revert_pr."
        ):
            return DEPLOYMENT
    return SENTINEL


def summarize(events: list[Event], cid: str, now: datetime) -> dict[str, Any]:
    status = chain_status(events, now)
    started, updated = events[0]["ts"], events[-1]["ts"]
    ended = None if status in ("running", "awaiting_review") else updated
    scenario = _earliest(events, "scenario")
    decided = _last(events, "agent.decided")
    decided_data = _data(decided) if decided is not None else {}
    escalation = _last(events, "escalation.raised")
    summary = decided_data.get("root_cause_summary")
    if summary is None and escalation is not None:
        summary = _data(escalation).get("summary")
    latest_eval = _last(events, "eval.scored")
    confidence = decided_data.get("confidence")
    return {
        "chain_id": cid,
        "title": _title(events, status, cid, scenario),
        "repo": _repo(events),
        "status": status,
        "scenario": scenario,
        "sha": _earliest(events, "sha"),
        "correlation_id": _earliest(events, "correlation_id"),
        "incident_id": _earliest(events, "incident_id"),
        "pr": _earliest(events, "pr"),
        "run_id": _earliest(events, "run_id"),
        "started_at": started,
        "updated_at": updated,
        "ended_at": ended,
        "duration_s": _secs(started, ended) if ended is not None else None,
        "stages": stages(events),
        "needs_action": _needs_action(events, status),
        "summary": summary,
        "resolution_type": decided_data.get("resolution_type"),
        "confidence": confidence if isinstance(confidence, int | float) else None,
        "eval_total": _score(latest_eval) if latest_eval is not None else None,
        "kinds": sorted({e["kind"] for e in events}),
        "event_count": len(events),
        "has_trace": bool(_of(events, "trace.summarized")),
        "has_eval": bool(_of(events, "eval.scored")),
    }


# ── Entity ────────────────────────────────────────────────────────────────────


def _agent(events: list[Event]) -> dict[str, Any] | None:
    decided = _last(events, "agent.decided")
    if decided is None:
        return None
    data = _data(decided)
    trace = _last(events, "trace.summarized")
    trace_url = data.get("langfuse_trace_url")
    if trace_url is None and trace is not None:
        trace_url = _data(trace).get("langfuse_trace_url")
    models = data.get("models_used")
    return {
        "decided_at": decided["ts"],
        "resolution_type": data.get("resolution_type"),
        "confidence": data.get("confidence"),
        "root_cause_summary": data.get("root_cause_summary"),
        "tool_calls": data.get("tool_calls"),
        "reflexion_loops": data.get("reflexion_loops"),
        "mttr_s": data.get("mttr_s"),
        "models_used": list(models) if isinstance(models, list) else [],
        "langfuse_trace_url": trace_url,
    }


def _trace(events: list[Event]) -> dict[str, Any] | None:
    trace = _last(events, "trace.summarized")
    if trace is None:
        return None
    data = _data(trace)
    spans = [
        {
            **span,
            "input_tokens": span.get("input_tokens"),
            "output_tokens": span.get("output_tokens"),
        }
        for span in data.get("spans") or []
    ]
    return {
        "trace_id": data.get("trace_id"),
        "langfuse_trace_url": data.get("langfuse_trace_url"),
        "total_duration_ms": data.get("total_duration_ms", 0),
        "spans": spans,
    }


def _complete(value: Any, names: tuple[str, ...]) -> dict[str, Any] | None:
    if isinstance(value, dict) and all(name in value for name in names):
        return {name: value[name] for name in names}
    return None


def _eval(event: Mapping[str, Any], scenario: Any) -> dict[str, Any]:
    data = _data(event)
    flagged = _flagged(event)
    return {
        "scored_at": event["ts"],
        "scenario": _chain(event).get("scenario") or scenario,
        "case": data.get("case"),
        "checks": _complete(data.get("checks"), CHECK_NAMES),
        "judge": None if flagged else _complete(data.get("judge"), JUDGE_NAMES),
        "total_score": _score(event),
        "judge_model": data.get("judge_model"),
        "judge_conflict": bool(data.get("judge_conflict")),
        "judge_failed": bool(data.get("judge_failed")),
        "langfuse_trace_url": data.get("langfuse_trace_url"),
    }


def _links(events: list[Event]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, label: str, url: Any, restricted: bool) -> None:
        if isinstance(url, str) and url not in seen and len(links) < MAX_LINKS:
            seen.add(url)
            links.append({"kind": kind, "label": label, "url": url, "restricted": restricted})

    for event in events:
        data = _data(event)
        if data.get("run_url"):
            label = RUN_LINK_LABELS.get(_source_file(event), "Workflow run")
            add("github_run", label, data["run_url"], False)
        if event["kind"].startswith("revert_pr.") and data.get("url"):
            add("github_pr", f"Revert PR #{_chain(event).get('pr')}", data["url"], False)
        if data.get("langfuse_trace_url"):
            add("langfuse", "LangFuse trace", data["langfuse_trace_url"], True)
        if data.get("monitor_url"):
            add("datadog", "Datadog monitor", data["monitor_url"], True)
    return links


def entity(
    events: list[Event], summary: dict[str, Any], generated_at: str, warn: Warn = warn
) -> dict[str, Any]:
    kept = events
    if len(kept) > MAX_ENTITY_EVENTS:
        warn(f"{summary['chain_id']}: {len(kept)} events, keeping the newest {MAX_ENTITY_EVENTS}")
        kept = kept[-MAX_ENTITY_EVENTS:]
    evals = [_eval(e, summary["scenario"]) for e in _of(events, "eval.scored")][-MAX_EVALS:]
    return {
        "schema": SNAPSHOT_SCHEMA,
        "file": "entity",
        "generated_at": generated_at,
        "chain_id": summary["chain_id"],
        "chain": summary,
        "events": kept,
        "agent": _agent(events),
        "trace": _trace(events),
        "evals": evals,
        "links": _links(events),
    }


# ── Metrics ───────────────────────────────────────────────────────────────────


def _incident_key(event: Mapping[str, Any], chain_id_of: Mapping[str, str]) -> str:
    """The incident an event belongs to: its correlation_id, else its chain (one deploy chain
    can hold several incidents, so incidents are not counted per chain)."""
    correlation = _chain(event).get("correlation_id")
    if correlation is not None:
        return f"correlation_id:{correlation}"
    return f"chain:{chain_id_of[event['event_id']]}"


def _mttr_values(
    decided: list[Event], first_alert: Mapping[str, str], incident_of: Mapping[str, str]
) -> list[float]:
    values: list[float] = []
    for event in decided:
        mttr = _data(event).get("mttr_s")
        if isinstance(mttr, int | float):
            values.append(float(mttr))
            continue
        alert_ts = first_alert.get(incident_of[event["event_id"]])
        if alert_ts is not None and alert_ts <= event["ts"]:
            values.append(_secs(alert_ts, event["ts"]))
    return values


def _scores(events: Iterable[Event]) -> list[float]:
    return [s for s in (_score(e) for e in _of(events, "eval.scored")) if s is not None]


def _kpis(
    events: list[Event], incident_of: Mapping[str, str], first_alert: Mapping[str, str]
) -> dict[str, Any]:
    deploys = len(_of(events, "deploy.succeeded", "deploy.failed"))
    deploy_failures = len(_of(events, "deploy.failed"))
    mttr = _mttr_values(_of(events, "agent.decided"), first_alert, incident_of)
    return {
        "deploys": deploys,
        "deploy_failures": deploy_failures,
        "failure_rate": deploy_failures / deploys if deploys else None,
        "builds": len(_of(events, "build.succeeded", "build.failed")),
        "build_failures": len(_of(events, "build.failed")),
        "incidents": len({incident_of[e["event_id"]] for e in _of(events, "alert.received")}),
        "mttr_p50_s": nearest_rank(mttr, 0.5),
        "mttr_p90_s": nearest_rank(mttr, 0.9),
        "revert_prs_opened": len(_of(events, "revert_pr.opened")),
        "revert_prs_merged": len(_of(events, "revert_pr.merged")),
        "escalations": len(_of(events, "escalation.raised")),
        "eval_runs": len(_of(events, "eval.scored")),
        "eval_score_mean": _mean(_scores(events)),
    }


def _daily(
    events: list[Event], incident_of: Mapping[str, str], first_alert: Mapping[str, str]
) -> list[dict[str, Any]]:
    by_date: dict[str, list[Event]] = {}
    for event in events:
        by_date.setdefault(event["ts"][:10], []).append(event)
    points = []
    for date in sorted(by_date)[-MAX_DAILY:]:
        day = by_date[date]
        points.append(
            {
                "date": date,
                "deploys": len(_of(day, "deploy.succeeded", "deploy.failed")),
                "deploy_failures": len(_of(day, "deploy.failed")),
                "incidents": len({incident_of[e["event_id"]] for e in _of(day, "alert.received")}),
                "escalations": len(_of(day, "escalation.raised")),
                "revert_prs_merged": len(_of(day, "revert_pr.merged")),
                "mttr_p50_s": nearest_rank(
                    _mttr_values(_of(day, "agent.decided"), first_alert, incident_of), 0.5
                ),
                "eval_score_mean": _mean(_scores(day)),
            }
        )
    return points


def _eval_scenarios(events: list[Event], scenario_of: Mapping[str, Any]) -> list[dict[str, Any]]:
    groups: dict[str, list[Event]] = {}
    for event in _of(events, "eval.scored"):
        scenario = _chain(event).get("scenario") or scenario_of.get(event["event_id"])
        if scenario:
            groups.setdefault(str(scenario), []).append(event)
    out = []
    for scenario in sorted(groups)[:MAX_SCENARIOS]:
        runs = groups[scenario]
        latest = runs[-1]
        checked = [c for c in (_complete(_data(e).get("checks"), CHECK_NAMES) for e in runs) if c]
        rate = (
            {name: sum(float(c[name]) for c in checked) / len(checked) for name in CHECK_NAMES}
            if checked
            else None
        )
        out.append(
            {
                "scenario": scenario,
                "case": _data(latest).get("case"),
                "runs": len(runs),
                "last_scored_at": latest["ts"],
                "last_total_score": _score(latest),
                "mean_total_score": _mean(_scores(runs)),
                "checks_pass_rate": rate,
                "judge_failures": sum(1 for e in runs if _data(e).get("judge_failed")),
                "judge_conflicts": sum(1 for e in runs if _data(e).get("judge_conflict")),
            }
        )
    return out


def metrics(
    chains: list[tuple[dict[str, Any], list[Event]]], events: list[Event], now: datetime
) -> dict[str, Any]:
    chain_id_of: dict[str, str] = {}
    scenario_of: dict[str, Any] = {}
    for summary, members in chains:
        for event in members:
            chain_id_of[event["event_id"]] = summary["chain_id"]
            scenario_of[event["event_id"]] = summary["scenario"]
    incident_of: dict[str, str] = {}
    first_alert: dict[str, str] = {}  # incident key → earliest alert.received ts
    for _, members in chains:
        for event in members:
            key = incident_of[event["event_id"]] = _incident_key(event, chain_id_of)
            if event["kind"] == "alert.received" and (
                key not in first_alert or event["ts"] < first_alert[key]
            ):
                first_alert[key] = event["ts"]
    ranges = {}
    for name, span in WINDOWS.items():
        start = _fmt(now - span) if span is not None else None
        window = [e for e in events if start is None or e["ts"] >= start]
        ranges[name] = _kpis(window, incident_of, first_alert)
    lanes = [s["needs_action"] for s, _ in chains if s["needs_action"] is not None]
    awaiting = [a["since"] for a in lanes if a["reason"] == "pr_awaiting_review"]
    return {
        "schema": SNAPSHOT_SCHEMA,
        "file": "metrics",
        "generated_at": _fmt(now),
        "ranges": ranges,
        "current": {
            "revert_prs_awaiting_review": len(awaiting),
            "oldest_awaiting_since": min(awaiting) if awaiting else None,
            "escalations_open": sum(1 for a in lanes if a["reason"] == "escalation"),
            "failed_no_incident": sum(1 for a in lanes if a["reason"] == "failed_no_incident"),
        },
        "daily": _daily(events, incident_of, first_alert),
        "eval": {"scenarios": _eval_scenarios(events, scenario_of)},
    }


# ── Documents ─────────────────────────────────────────────────────────────────


def _sources(status: Mapping[str, str] | None) -> dict[str, str]:
    sources = dict(DEFAULT_SOURCES)
    for key in sources:
        value = (status or {}).get(key)
        if value in SOURCE_STATUSES:
            sources[key] = str(value)
    return sources


def meta(
    events: list[Event],
    chain_count: int,
    *,
    now: datetime,
    status: Mapping[str, str] | None,
    run_url: str | None,
    ledger_head: str | None,
) -> dict[str, Any]:
    return {
        "schema": SNAPSHOT_SCHEMA,
        "file": "meta",
        "generated_at": _fmt(now),
        "ledger_head": ledger_head,
        "event_count": len(events),
        "chain_count": chain_count,
        "first_event_at": events[0]["ts"] if events else None,
        "last_event_at": events[-1]["ts"] if events else None,
        "reconciler_run_url": run_url or None,
        "sources": _sources(status),
    }


def build(
    ledger_dir: Path,
    *,
    now: datetime,
    status: Mapping[str, str] | None,
    run_url: str | None,
    ledger_head: str | None,
    warn: Warn = warn,
) -> dict[str, dict[str, Any]]:
    """Relative path -> document for every snapshot file."""
    generated_at = _fmt(now)
    events = load_events(ledger_dir, warn)
    chains: list[tuple[dict[str, Any], list[Event]]] = []
    for members in group_chains(events):
        cid = chain_id(members)
        chains.append((summarize(members, cid, now), members))
    chains.sort(key=lambda item: (item[0]["started_at"], item[0]["chain_id"]), reverse=True)
    docs: dict[str, dict[str, Any]] = {
        "meta.json": meta(
            events,
            len(chains),
            now=now,
            status=status,
            run_url=run_url,
            ledger_head=ledger_head,
        ),
        "chains.json": {
            "schema": SNAPSHOT_SCHEMA,
            "file": "chains",
            "generated_at": generated_at,
            "chains": [summary for summary, _ in chains],
        },
        "metrics.json": metrics(chains, events, now),
    }
    for summary, members in chains:
        docs[f"entities/{summary['chain_id']}.json"] = entity(members, summary, generated_at, warn)
    return docs


def validate_docs(docs: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """`<file>: <error>` for every document (and entity event) that fails its schema."""
    snapshot_schema = ledger.load_schema(ledger.SNAPSHOT_SCHEMA_PATH)
    event_schema = ledger.load_schema(ledger.EVENT_SCHEMA_PATH)
    errors: list[str] = []
    for name, doc in docs.items():
        errors += [f"{name}: {e}" for e in ledger.validate(doc, snapshot_schema)]
        if doc.get("file") == "entity":
            for i, event in enumerate(doc.get("events") or []):
                errors += [f"{name}: /events/{i}{e}" for e in ledger.validate(event, event_schema)]
    return errors


def dumps(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"


def write(out_dir: Path, docs: Mapping[str, Mapping[str, Any]]) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    for name, doc in docs.items():
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps(doc), encoding="utf-8")


def _ledger_head(ledger_dir: Path) -> str | None:
    if not (ledger_dir / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(ledger_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = proc.stdout.strip()
    return head if proc.returncode == 0 and len(head) == 40 else None


def _read_status(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(status, dict) and status.get("file") == "meta":  # a previous snapshot's meta.json
        status = status.get("sources")
    return status if isinstance(status, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snapshot.py", description="Sentinel ops snapshot")
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    if args.now:
        try:
            now = datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            print("ops-snapshot: --now is not an RFC 3339 timestamp", file=sys.stderr)
            return 2
    else:
        now = datetime.now(UTC)
    now = now.replace(microsecond=0)
    docs = build(
        args.ledger,
        now=now,
        status=_read_status(args.status),
        run_url=args.run_url or None,
        ledger_head=_ledger_head(args.ledger),
    )
    errors = validate_docs(docs)
    if errors:
        for error in errors[:50]:
            print(f"ops-snapshot: {error}", file=sys.stderr)
        return 2
    write(args.out, docs)
    print(f"ops-snapshot: {len(docs)} files, {docs['meta.json']['event_count']} events")
    return 0


if __name__ == "__main__":
    sys.exit(main())
