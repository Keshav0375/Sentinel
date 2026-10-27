"""scripts/ops/snapshot.py: chains, metrics and entities from a ledger clone (backend task 10.3).

Events are built with rules.py / ledger.build_event (the writers' own recipes), written into a temp
ledger directory exactly where ledger.append would put them, and the snapshot is checked against
the frozen snapshot.v1 schema. Nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts/ops"
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
SHA = "4f1d2a9c0b8e7d6f5a4b3c2d1e0f9a8b7c6d5e4f"
MAIN_SHA = "9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b"
CORR = "6f9619ff-8b86-4011-b42d-00cf4fc964ff"
CORR_2 = "2a6c1f3e-9b7d-4e2a-8c5f-1d3b7e9a0c42"
CORR_3 = "7d3e5f60-1a2b-4c3d-9e8f-0a1b2c3d4e5f"
INCIDENT = "0b6c3d2e-1f4a-4b5c-8d9e-0a1b2c3d4e5f"
APP = "gha:Keshav0375/Sentinel-deployment/ci_app_deployment.yml"
INCIDENT_WF = "gha:Keshav0375/Sentinel/ci_incident_response.yml"
EVAL_WF = "gha:Keshav0375/Sentinel/ci_eval.yml"
DEPLOY_RUN = "https://github.com/Keshav0375/Sentinel-deployment/actions/runs/18800000202"
INCIDENT_RUN = "https://github.com/Keshav0375/Sentinel/actions/runs/18800000405"
PR_URL = "https://github.com/Keshav0375/Sentinel-deployment/pull/57"
TRACE_URL = (
    "https://cloud.langfuse.com/project/sentinel-dev/traces/6f9619ff8b864011b42d00cf4fc964ff"
)
MONITOR = "https://app.datadoghq.com/monitors/123456"


@pytest.fixture(scope="session")
def snapshot() -> ModuleType:
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    spec = importlib.util.spec_from_file_location("ops_snapshot", OPS_DIR / "snapshot.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ops_snapshot"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def rules(snapshot: ModuleType) -> ModuleType:
    return importlib.import_module("rules")


def _at(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(
    snapshot: ModuleType,
    kind: str,
    source: str,
    source_id: str,
    ts: str,
    chain: dict[str, Any],
    data: dict[str, Any],
    state: str = "",
) -> dict[str, Any]:
    recorded = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    event: dict[str, Any] = snapshot.ledger.build_event(
        kind=kind,
        source=source,
        source_id=source_id,
        state=state,
        ts=ts,
        chain=chain,
        data=data,
        recorded_at=recorded + timedelta(seconds=5),
    )
    return event


def spec_event(snapshot: ModuleType, spec: Any) -> dict[str, Any]:
    recorded = datetime.strptime(spec.ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    event: dict[str, Any] = spec.build(recorded_at=recorded)
    return event


def write_ledger(snapshot: ModuleType, root: Path, events: list[dict[str, Any]]) -> Path:
    for event in events:
        path = root / snapshot.ledger.event_path(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(snapshot.ledger.serialize(event))
    return root


# ── Event builders ────────────────────────────────────────────────────────────


def app_failed(
    snapshot: ModuleType, ts: str, run_id: int = 18800000202, stage: str = "verify"
) -> dict:
    return ev(
        snapshot,
        "deploy.failed",
        APP,
        f"{run_id}.1",
        ts,
        {"sha": SHA, "run_id": run_id, "scenario": "runtime/04"},
        {
            "version": "pr-61-4f1d2a9",
            "run_url": f"https://github.com/Keshav0375/Sentinel-deployment/actions/runs/{run_id}",
            "failed_stage": stage,
            "duration_s": 120,
        },
    )


def app_succeeded(snapshot: ModuleType, ts: str, run_id: int, sha: str) -> dict:
    return ev(
        snapshot,
        "deploy.succeeded",
        APP,
        f"{run_id}.1",
        ts,
        {"sha": sha, "run_id": run_id},
        {
            "version": f"pr-60-{sha[:7]}",
            "run_url": f"https://github.com/Keshav0375/Sentinel-deployment/actions/runs/{run_id}",
            "duration_s": 300,
        },
    )


def alert(snapshot: ModuleType, ts: str, corr: str = CORR) -> dict:
    return ev(
        snapshot,
        "alert.received",
        INCIDENT_WF,
        corr,
        ts,
        {"correlation_id": corr},
        {
            "signal_type": "runtime_error",
            "service": "sentinel-app",
            "severity": "P2",
            "monitor_url": MONITOR,
        },
        state="runtime_error",
    )


def agent_started(snapshot: ModuleType, ts: str, corr: str = CORR) -> dict:
    return ev(
        snapshot,
        "agent.started",
        INCIDENT_WF,
        corr,
        ts,
        {"correlation_id": corr},
        {"run_url": INCIDENT_RUN},
    )


def decided(
    snapshot: ModuleType,
    ts: str,
    resolution: str = "rollback",
    corr: str = CORR,
    **data: Any,
) -> dict:
    chain: dict[str, Any] = {"correlation_id": corr}
    if resolution == "rollback":
        chain.update({"sha": SHA, "incident_id": INCIDENT})
    return ev(
        snapshot,
        "agent.decided",
        INCIDENT_WF,
        corr,
        ts,
        chain,
        {
            "resolution_type": resolution,
            "confidence": 0.87,
            "root_cause_summary": "The health route regressed in pr-61.",
            "run_url": INCIDENT_RUN,
            **data,
        },
        state=resolution,
    )


def revert(snapshot: ModuleType, kind: str, ts: str, review_id: str = "", **data: Any) -> dict:
    return ev(
        snapshot,
        kind,
        "reconciler",
        "Keshav0375/Sentinel-deployment#57",
        ts,
        {"pr": 57, "sha": SHA[:7], "incident_id": INCIDENT},
        {"url": PR_URL, **data},
        state=review_id,
    )


def eval_scored(
    snapshot: ModuleType, ts: str, run_id: int, total: float, *, failed: bool = False
) -> dict:
    return ev(
        snapshot,
        "eval.scored",
        EVAL_WF,
        f"{run_id}.1",
        ts,
        {"correlation_id": CORR, "scenario": "runtime/04"},
        {
            "case": "ii",
            "checks": {"fired": 1, "classified": 1, "correlated": 0, "revert_target": 1},
            "total_score": total,
            "judge_model": "claude-opus-4-1",
            "judge_failed": failed,
            "judge_conflict": False,
            "run_url": f"https://github.com/Keshav0375/Sentinel/actions/runs/{run_id}",
        },
        state="runtime/04",
    )


def rollback_chain(snapshot: ModuleType) -> list[dict]:
    return [
        app_failed(snapshot, _at(240)),
        alert(snapshot, _at(235)),
        agent_started(snapshot, _at(234)),
        decided(snapshot, _at(230)),
        revert(snapshot, "revert_pr.opened", _at(229), state="open"),
    ]


def _summaries(snapshot: ModuleType, events: list[dict]) -> list[dict]:
    ordered = sorted(events, key=lambda e: (e["ts"], e["recorded_at"], e["event_id"]))
    return [
        snapshot.summarize(members, snapshot.chain_id(members), NOW)
        for members in snapshot.group_chains(ordered)
    ]


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_empty_ledger(
    snapshot: ModuleType, tmp_path: Path, snapshot_schema: dict[str, Any]
) -> None:
    docs = snapshot.build(tmp_path / "absent", now=NOW, status=None, run_url=None, ledger_head=None)

    assert set(docs) == {"meta.json", "chains.json", "metrics.json"}
    assert docs["chains.json"]["chains"] == []
    meta = docs["meta.json"]
    assert meta["event_count"] == 0 and meta["chain_count"] == 0
    assert meta["ledger_head"] is None and meta["first_event_at"] is None
    assert meta["sources"] == {"github": "ok", "langfuse": "skipped"}
    assert docs["metrics.json"]["ranges"]["all"]["failure_rate"] is None
    assert docs["metrics.json"]["daily"] == []
    for doc in docs.values():
        assert snapshot.ledger.validate(doc, snapshot_schema) == []
    assert snapshot.validate_docs(docs) == []


def test_rollback_chain_groups_across_sources(snapshot: ModuleType) -> None:
    (summary,) = _summaries(snapshot, rollback_chain(snapshot))

    expected = "c_" + hashlib.sha256(f"correlation_id:{CORR}".encode()).hexdigest()[:16]
    assert summary["chain_id"] == expected
    assert summary["status"] == "awaiting_review"
    assert summary["repo"] == "Keshav0375/Sentinel-deployment"
    assert summary["needs_action"] == {
        "reason": "pr_awaiting_review",
        "since": _at(229),
        "url": PR_URL,
    }
    assert summary["title"] == "runtime/04: revert PR #57 awaiting review"
    assert summary["ended_at"] is None and summary["duration_s"] is None
    assert (summary["sha"], summary["pr"], summary["run_id"]) == (SHA, 57, 18800000202)
    assert summary["incident_id"] == INCIDENT
    assert summary["summary"] == "The health route regressed in pr-61."
    assert (summary["resolution_type"], summary["confidence"]) == ("rollback", 0.87)
    assert [s["name"] for s in summary["stages"]] == [
        "deploy",
        "alert",
        "agent",
        "pr",
        "review",
        "merge",
    ]
    stages = {s["name"]: s for s in summary["stages"]}
    assert stages["deploy"]["status"] == "failed"
    assert stages["deploy"]["label"] == "Failed at verify"
    assert stages["deploy"]["duration_s"] == 120
    assert stages["alert"]["label"] == "runtime_error, P2"
    assert stages["alert"]["url"] == MONITOR
    assert stages["agent"]["label"] == "Rollback, confidence 0.87"
    assert stages["pr"]["label"] == "Revert PR #57 opened"
    assert (stages["review"]["status"], stages["review"]["label"]) == ("waiting", "Awaiting review")
    assert stages["merge"]["status"] == "none"
    assert summary["kinds"] == sorted(
        {"deploy.failed", "alert.received", "agent.started", "agent.decided", "revert_pr.opened"}
    )


def _case_events(snapshot: ModuleType, rules: ModuleType, case: str) -> list[dict]:
    rollback = rollback_chain(snapshot)
    if case == "merged":
        return [
            *rollback,
            revert(
                snapshot,
                "revert_pr.review_submitted",
                _at(200),
                reviewer="Keshav0375",
                review_state="APPROVED",
                review_id="3100000002",
            ),
            revert(snapshot, "revert_pr.merged", _at(190), state="merged", reviewer="Keshav0375"),
        ]
    if case == "closed":
        return [*rollback, revert(snapshot, "revert_pr.closed", _at(190), state="closed")]
    if case == "escalation":
        return [
            alert(snapshot, _at(60)),
            decided(snapshot, _at(50), resolution="escalated"),
            ev(
                snapshot,
                "escalation.raised",
                INCIDENT_WF,
                CORR,
                _at(50),
                {"correlation_id": CORR},
                {"summary": "Unclear cause", "run_url": INCIDENT_RUN},
            ),
        ]
    if case == "agent_failed":
        return [
            alert(snapshot, _at(60)),
            agent_started(snapshot, _at(59)),
            ev(
                snapshot,
                "agent.failed",
                INCIDENT_WF,
                CORR,
                _at(40),
                {"correlation_id": CORR},
                {"error_class": "pipeline_fault", "run_url": INCIDENT_RUN},
            ),
        ]
    if case in ("running", "stale"):
        ts = _at(10 if case == "running" else 7 * 60)
        return [
            spec_event(
                snapshot,
                rules.backend_build_started(sha=MAIN_SHA, run_id=777, run_attempt=1, ts=ts),
            )
        ]
    if case == "failed_no_incident":
        return [app_failed(snapshot, _at(30))]
    facts = {
        "run-quality": rules.JobFact("success", _at(60), _at(55)),
        "build-and-push": rules.JobFact("success", _at(55), _at(50)),
        "deploy-to-aks": rules.JobFact("success", _at(50), _at(45)),
        "run-tests": rules.JobFact("success", _at(45), _at(40)),
    }
    specs = rules.backend_deploy_events(
        facts, sha=MAIN_SHA, run_id=778, run_attempt=1, with_started=True
    )
    return [spec_event(snapshot, s) for s in specs]


@pytest.mark.parametrize(
    ("case", "status", "stage_count", "reason"),
    [
        ("merged", "reverted", 6, None),
        ("closed", "closed", 6, None),
        ("escalation", "escalated", 4, "escalation"),
        ("agent_failed", "failed", 6, None),
        ("running", "running", 6, None),
        ("stale", "failed", 6, None),
        ("failed_no_incident", "failed", 6, "failed_no_incident"),
        ("succeeded", "succeeded", 6, None),
    ],
)
def test_status_priority(
    snapshot: ModuleType,
    rules: ModuleType,
    case: str,
    status: str,
    stage_count: int,
    reason: str | None,
) -> None:
    (summary,) = _summaries(snapshot, _case_events(snapshot, rules, case))

    assert summary["status"] == status
    assert len(summary["stages"]) == stage_count
    action = summary["needs_action"]
    assert (action["reason"] if action else None) == reason
    if case == "merged":
        stages = {s["name"]: s for s in summary["stages"]}
        assert stages["review"]["label"] == "Approved by Keshav0375"
        assert stages["merge"]["status"] == "succeeded"
        assert summary["title"] == "runtime/04: reverted by PR #57"
        assert summary["ended_at"] == _at(190)
    if case == "closed":
        assert {s["name"]: s for s in summary["stages"]}["merge"]["status"] == "skipped"
    if case == "escalation":
        assert summary["stages"][-1]["label"] == "Escalated to a human"
        assert action["url"] == INCIDENT_RUN
        assert summary["title"] == "escalated to a human"
    if case == "failed_no_incident":
        assert action == {"reason": "failed_no_incident", "since": _at(30), "url": DEPLOY_RUN}
        assert summary["title"] == "runtime/04: App deploy pr-61-4f1d2a9 failed at verify"
    if case == "running":
        assert summary["stages"][0]["label"] == f"Building sha-{MAIN_SHA[:7]}"
        assert summary["ended_at"] is None
    if case == "succeeded":
        assert summary["stages"][0]["label"] == f"Deployed sha-{MAIN_SHA[:7]}"
        assert summary["title"] == f"Backend deploy sha-{MAIN_SHA[:7]}"
        assert summary["duration_s"] == 20 * 60


def test_separate_chains(snapshot: ModuleType, rules: ModuleType) -> None:
    scale = rules.scale_events(
        event_name="schedule",
        direction="",
        result="success",
        keep_warm=None,
        run_id=900,
        run_attempt=1,
        ts=_at(30),
    )
    build = rules.backend_build_started(sha=MAIN_SHA, run_id=901, run_attempt=1, ts=_at(20))
    events = [spec_event(snapshot, s) for s in [*scale, build]]

    summaries = _summaries(snapshot, events)

    assert len(summaries) == 2
    titles = {s["title"] for s in summaries}
    assert titles == {"Backend scaled to 0", f"Backend deploy sha-{MAIN_SHA[:7]}"}
    scale_chain = next(s for s in summaries if s["title"] == "Backend scaled to 0")
    assert scale_chain["chain_id"] == "c_" + hashlib.sha256(b"run_id:900").hexdigest()[:16]
    assert scale_chain["stages"][0]["label"] == "Scaled to 0"


def test_infra_chain_titles(snapshot: ModuleType, rules: ModuleType) -> None:
    fact = rules.JobFact
    runner = rules.infra_build_events(
        fact("success", _at(90), _at(80)),
        sha=MAIN_SHA,
        run_id=501,
        run_attempt=1,
        with_started=True,
    )
    apply = rules.infra_deploy_events(
        fact("success", _at(70), _at(60)),
        fact("failure", _at(60), _at(50)),
        guards_passed=True,
        sha=MAIN_SHA,
        run_id=601,
        run_attempt=1,
        with_started=True,
    )
    backend = [rules.backend_build_started(sha=MAIN_SHA, run_id=602, run_attempt=1, ts=_at(5))]
    events = [spec_event(snapshot, s) for s in [*runner, *apply, *backend]]

    summaries = {s["title"]: s for s in _summaries(snapshot, events)}

    short = MAIN_SHA[:7]
    assert set(summaries) == {
        f"Runner image sha-{short}",
        f"Infra apply sha-{short} failed at verify",
        f"Backend deploy sha-{short}",
    }
    infra = summaries[f"Infra apply sha-{short} failed at verify"]
    assert infra["repo"] == "Keshav0375/Sentinel"
    assert infra["status"] == "failed"
    assert infra["needs_action"]["reason"] == "failed_no_incident"
    assert summaries[f"Runner image sha-{short}"]["status"] == "succeeded"


def _metric_events(snapshot: ModuleType) -> list[dict]:
    return [
        app_succeeded(snapshot, _at(60), 300, MAIN_SHA),
        app_failed(snapshot, _at(120), run_id=301),
        app_succeeded(snapshot, _at(3 * 24 * 60), 302, "1" * 40),
        alert(snapshot, _at(500), corr=CORR),
        decided(snapshot, _at(495), resolution="escalated", corr=CORR),  # fallback: 300 s
        alert(snapshot, _at(400), corr=CORR_2),
        decided(snapshot, _at(385), resolution="escalated", corr=CORR_2),  # fallback: 900 s
        alert(snapshot, _at(300), corr=CORR_3),
        decided(snapshot, _at(250), resolution="escalated", corr=CORR_3, mttr_s=100),
        eval_scored(snapshot, _at(30), 40, 0.8),
        eval_scored(snapshot, _at(20), 41, 0.6),
        eval_scored(snapshot, _at(10), 42, 0.1, failed=True),
    ]


def test_metrics(snapshot: ModuleType, tmp_path: Path) -> None:
    ledger_dir = write_ledger(snapshot, tmp_path / "ledger", _metric_events(snapshot))

    metrics = snapshot.build(ledger_dir, now=NOW, status=None, run_url=None, ledger_head=None)[
        "metrics.json"
    ]

    day, all_time = metrics["ranges"]["24h"], metrics["ranges"]["all"]
    assert (day["deploys"], day["deploy_failures"], day["failure_rate"]) == (2, 1, 0.5)
    assert all_time["deploys"] == 3
    assert metrics["ranges"]["7d"]["deploys"] == 3
    assert all_time["incidents"] == 3
    assert (all_time["mttr_p50_s"], all_time["mttr_p90_s"]) == (300.0, 900.0)
    assert all_time["eval_runs"] == 3
    assert all_time["eval_score_mean"] == pytest.approx(0.7)
    assert snapshot.nearest_rank([5.0, 1.0, 3.0, 2.0], 0.5) == 2.0
    assert snapshot.nearest_rank([], 0.9) is None
    assert [p["date"] for p in metrics["daily"]] == ["2026-10-22", "2026-10-25"]
    assert metrics["daily"][-1]["deploys"] == 2
    (scenario,) = metrics["eval"]["scenarios"]
    assert scenario["scenario"] == "runtime/04"
    assert scenario["runs"] == 3
    assert scenario["last_total_score"] is None  # judge_failed
    assert scenario["mean_total_score"] == pytest.approx(0.7)
    assert scenario["judge_failures"] == 1
    assert scenario["checks_pass_rate"] == {
        "fired": 1,
        "classified": 1,
        "correlated": 0,
        "revert_target": 1,
    }
    assert metrics["current"]["escalations_open"] == 0  # escalated decisions, no escalation.raised


def _kpis_of(snapshot: ModuleType, events: list[dict]) -> dict[str, Any]:
    ordered = sorted(events, key=lambda e: (e["ts"], e["recorded_at"], e["event_id"]))
    chains = []
    for members in snapshot.group_chains(ordered):
        cid = snapshot.chain_id(members)
        chains.append((snapshot.summarize(members, cid, NOW), members))
    out: dict[str, Any] = snapshot.metrics(chains, ordered, NOW)
    return out


def test_two_incidents_same_sha(snapshot: ModuleType) -> None:
    """One deploy sha, two incidents two days apart: one chain, but two incidents and per-incident
    MTTR (not decided − the chain's first alert)."""
    day_1, day_2 = "2026-10-21T00:00:00Z", "2026-10-23T00:00:00Z"
    events = [
        alert(snapshot, day_1, corr=CORR),
        decided(snapshot, "2026-10-21T00:05:00Z", corr=CORR),
        alert(snapshot, day_2, corr=CORR_2),
        decided(snapshot, "2026-10-23T00:05:00Z", corr=CORR_2),
    ]
    assert all(e["chain"].get("sha") == SHA for e in events if e["kind"] == "agent.decided")

    metrics = _kpis_of(snapshot, events)

    assert len(snapshot.group_chains(events)) == 1
    all_time = metrics["ranges"]["all"]
    assert all_time["incidents"] == 2
    assert (all_time["mttr_p50_s"], all_time["mttr_p90_s"]) == (300.0, 300.0)
    assert {p["date"]: p["incidents"] for p in metrics["daily"]} == {
        "2026-10-21": 1,
        "2026-10-23": 1,
    }


def test_incident_without_correlation_falls_back_to_chain(snapshot: ModuleType) -> None:
    chain = {"incident_id": INCIDENT, "sha": SHA}
    events = [
        {**alert(snapshot, _at(100)), "chain": chain},
        {**decided(snapshot, _at(90), resolution="escalated"), "chain": chain},
    ]

    all_time = _kpis_of(snapshot, events)["ranges"]["all"]

    assert all_time["incidents"] == 1
    assert all_time["mttr_p50_s"] == 600.0


def test_open_stage_rerun_same_run_id(snapshot: ModuleType) -> None:
    """A re-run (same run_id, new attempt) after a terminal is open until its own terminal."""
    backend = "gha:Keshav0375/Sentinel/ci_backend_deployment.yml"
    chain = {"sha": MAIN_SHA, "run_id": 9}
    run = {"run_url": "https://github.com/Keshav0375/Sentinel/actions/runs/9"}
    t0, t1, t2, t3 = _at(30), _at(25), _at(20), _at(10)
    events = [
        ev(snapshot, "build.started", backend, "9.1", t0, chain, run),
        ev(snapshot, "build.failed", backend, "9.1", t1, chain, {**run, "failed_stage": "test"}),
        ev(snapshot, "build.started", backend, "9.2", t2, chain, run),
    ]
    at_t2 = datetime.strptime(t2, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    now = at_t2 + timedelta(seconds=60)

    assert snapshot.chain_status(events, now) == "running"
    events.append(ev(snapshot, "build.succeeded", backend, "9.2", t3, chain, run))
    assert snapshot.chain_status(events, now) == "succeeded"


def _agent_failed(snapshot: ModuleType, ts: str) -> dict:
    return ev(
        snapshot,
        "agent.failed",
        INCIDENT_WF,
        CORR,
        ts,
        {"correlation_id": CORR},
        {"error_class": "pipeline_fault", "run_url": INCIDENT_RUN},
    )


def test_later_decision_beats_earlier_failure(snapshot: ModuleType) -> None:
    """A re-run that decided a rollback after an earlier agent.failed is judged by its decision."""
    events = [
        alert(snapshot, _at(60)),
        _agent_failed(snapshot, _at(50)),
        decided(snapshot, _at(20)),  # rollback, no revert PR yet
    ]

    assert snapshot.chain_status(events, NOW) == "running"
    opened = [*events, revert(snapshot, "revert_pr.opened", _at(10), state="open")]
    assert snapshot.chain_status(opened, NOW) == "awaiting_review"
    # The reverse order still fails: the latest outcome is the failure.
    reverse = [
        alert(snapshot, _at(60)),
        decided(snapshot, _at(50)),
        _agent_failed(snapshot, _at(20)),
    ]
    assert snapshot.chain_status(reverse, NOW) == "failed"


def test_deploy_with_tests_skipped_is_terminal(snapshot: ModuleType, rules: ModuleType) -> None:
    fact = rules.JobFact
    jobs = {
        "run-quality": fact("success", _at(9 * 60), _at(9 * 60 - 3)),
        "build-and-push": fact("success", _at(9 * 60 - 3), _at(9 * 60 - 10)),
        "deploy-to-aks": fact("success", _at(9 * 60 - 10), _at(9 * 60 - 20)),
        "run-tests": fact("skipped"),
    }
    specs = rules.backend_deploy_events(
        jobs, sha=MAIN_SHA, run_id=778, run_attempt=1, with_started=True
    )
    events = [spec_event(snapshot, s) for s in specs]

    # Nine hours later: past STALE_RUNNING_S, yet a finished deploy is neither running nor stale.
    assert snapshot.chain_status(events, NOW) == "succeeded"
    (summary,) = _summaries(snapshot, events)
    assert summary["ended_at"] is not None
    assert summary["stages"][0]["status"] == "succeeded"


def test_entity_contents(snapshot: ModuleType, tmp_path: Path) -> None:
    trace = ev(
        snapshot,
        "trace.summarized",
        "langfuse",
        "6f9619ff8b864011b42d00cf4fc964ff",
        _at(228),
        {"incident_id": INCIDENT, "correlation_id": CORR},
        {
            "trace_id": "6f9619ff8b864011b42d00cf4fc964ff",
            "langfuse_trace_url": TRACE_URL,
            "total_duration_ms": 9000,
            "spans": [
                {
                    "span_id": "root",
                    "parent_id": None,
                    "name": "incident-pipeline",
                    "kind": "SPAN",
                    "start_ms": 0,
                    "duration_ms": 9000,
                    "status": "ok",
                },
                {
                    "span_id": "triage",
                    "parent_id": "root",
                    "name": "triage",
                    "kind": "GENERATION",
                    "start_ms": 100,
                    "duration_ms": 2000,
                    "status": "error",
                    "input_tokens": 1200,
                    "output_tokens": 80,
                },
            ],
        },
    )
    events = [
        *rollback_chain(snapshot)[:3],
        decided(snapshot, _at(230), models_used=["claude-sonnet-4-5"], tool_calls=7),
        rollback_chain(snapshot)[4],
        trace,
        eval_scored(snapshot, _at(100), 40, 0.8),
    ]
    ledger_dir = write_ledger(snapshot, tmp_path / "ledger", events)

    docs = snapshot.build(ledger_dir, now=NOW, status=None, run_url=None, ledger_head=None)

    (name,) = [n for n in docs if n.startswith("entities/")]
    entity = docs[name]
    assert entity["chain_id"] == name[len("entities/") : -len(".json")]
    assert [e["event_id"] for e in entity["events"]] == [
        e["event_id"] for e in sorted(events, key=lambda e: e["ts"])
    ]
    agent = entity["agent"]
    assert agent["resolution_type"] == "rollback"
    assert agent["models_used"] == ["claude-sonnet-4-5"]
    assert (agent["tool_calls"], agent["reflexion_loops"], agent["mttr_s"]) == (7, None, None)
    assert agent["langfuse_trace_url"] == TRACE_URL  # from the trace
    spans = entity["trace"]["spans"]
    assert (spans[0]["input_tokens"], spans[0]["output_tokens"]) == (None, None)
    assert spans[1]["input_tokens"] == 1200
    (score,) = entity["evals"]
    assert score["scenario"] == "runtime/04"
    assert score["total_score"] == 0.8
    assert score["judge"] is None  # no judge breakdown recorded
    assert score["checks"]["fired"] == 1
    links = {(link["kind"], link["label"]): link for link in entity["links"]}
    assert links[("github_run", "Deployment run")]["restricted"] is False
    assert links[("github_run", "Incident response run")]["url"] == INCIDENT_RUN
    assert links[("github_pr", "Revert PR #57")]["url"] == PR_URL
    assert links[("langfuse", "LangFuse trace")]["restricted"] is True
    assert links[("datadog", "Datadog monitor")]["restricted"] is True
    urls = [link["url"] for link in entity["links"]]
    assert len(urls) == len(set(urls))
    assert entity["chain"]["has_trace"] and entity["chain"]["has_eval"]
    assert entity["chain"]["eval_total"] == 0.8


def _everything(snapshot: ModuleType, rules: ModuleType) -> list[dict]:
    events: list[dict] = []
    for case in ("merged", "escalation", "running", "succeeded"):
        events += _case_events(snapshot, rules, case)
    events += _metric_events(snapshot)[:3]
    return events


def test_outputs_validate(
    snapshot: ModuleType,
    rules: ModuleType,
    tmp_path: Path,
    snapshot_schema: dict[str, Any],
    event_schema: dict[str, Any],
) -> None:
    ledger_dir = write_ledger(snapshot, tmp_path / "ledger", _everything(snapshot, rules))

    docs = snapshot.build(
        ledger_dir,
        now=NOW,
        status={"github": "error", "langfuse": "ok"},
        run_url="https://github.com/Keshav0375/Sentinel/actions/runs/18800000999",
        ledger_head="a" * 40,
    )

    assert snapshot.validate_docs(docs) == []
    entities = [n for n in docs if n.startswith("entities/")]
    assert len(entities) == docs["meta.json"]["chain_count"] == len(docs["chains.json"]["chains"])
    assert len(entities) >= 3
    for name, doc in docs.items():
        assert snapshot.ledger.validate(doc, snapshot_schema) == [], name
        assert snapshot.ledger.find_secrets(doc) == [], name
        for event in doc.get("events", []):
            assert snapshot.ledger.validate(event, event_schema) == []
    assert docs["meta.json"]["sources"] == {"github": "error", "langfuse": "ok"}
    started = [c["started_at"] for c in docs["chains.json"]["chains"]]
    assert started == sorted(started, reverse=True)


def test_validation_error_blocks_the_write(
    snapshot: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"

    rc = snapshot.main(
        ["--ledger", str(tmp_path / "absent"), "--out", str(out), "--run-url", "not-a-url"]
    )

    assert rc == 2
    assert not out.exists()
    assert "ops-snapshot: meta.json: /reconciler_run_url" in capsys.readouterr().err


def test_cli_writes_dir(
    snapshot: ModuleType,
    rules: ModuleType,
    tmp_path: Path,
    git_bin: str,
    env_without_git: dict[str, str],
) -> None:
    ledger_dir = write_ledger(snapshot, tmp_path / "ledger", _everything(snapshot, rules))
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *args], cwd=ledger_dir, env=env_without_git, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x"],
        cwd=ledger_dir,
        env=env_without_git,
        check=True,
    )
    status = tmp_path / "status.json"
    status.write_text('{"github": "ok", "langfuse": "error"}\n')
    out = tmp_path / "snapshot"
    stale = out / "entities" / "c_0000000000000000.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    argv = ["--ledger", str(ledger_dir), "--out", str(out), "--status", str(status)]
    argv += ["--run-url", "https://github.com/Keshav0375/Sentinel/actions/runs/1"]
    argv += ["--now", "2026-10-25T12:00:00Z"]

    assert snapshot.main(argv) == 0

    chains = json.loads((out / "chains.json").read_text())["chains"]
    entities = {p.name for p in (out / "entities").iterdir()}
    assert entities == {f"{c['chain_id']}.json" for c in chains}
    assert not stale.exists()
    meta_text = (out / "meta.json").read_text()
    assert meta_text.endswith("}\n") and ", " not in meta_text and '": ' not in meta_text
    meta = json.loads(meta_text)
    assert meta["sources"] == {"github": "ok", "langfuse": "error"}
    assert meta["reconciler_run_url"] == "https://github.com/Keshav0375/Sentinel/actions/runs/1"
    head = subprocess.run(
        ["git", "-C", str(ledger_dir), "rev-parse", "HEAD"],
        env=env_without_git,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert meta["ledger_head"] == head
    assert meta["generated_at"] == "2026-10-25T12:00:00Z"
    assert snapshot.main(argv) == 0
    assert {p.name for p in (out / "entities").iterdir()} == entities


def test_status_carried_from_previous_meta(snapshot: ModuleType, tmp_path: Path) -> None:
    """A Pages rebuild passes the last reconcile's meta.json: its sources are carried over."""
    previous = tmp_path / "last" / "meta.json"
    previous.parent.mkdir()
    reconciled = snapshot.build(
        tmp_path / "absent",
        now=NOW,
        status={"github": "error", "langfuse": "ok"},
        run_url=None,
        ledger_head=None,
    )["meta.json"]
    previous.write_text(json.dumps(reconciled))
    out = tmp_path / "snapshot"

    argv = ["--ledger", str(tmp_path / "absent"), "--out", str(out), "--status", str(previous)]
    assert snapshot.main([*argv, "--now", "2026-10-25T12:30:00Z"]) == 0

    meta = json.loads((out / "meta.json").read_text())
    assert meta["sources"] == {"github": "error", "langfuse": "ok"}
    assert meta["generated_at"] == "2026-10-25T12:30:00Z"
    assert snapshot._read_status(tmp_path / "missing.json") is None


def test_invalid_event_skipped(snapshot: ModuleType, tmp_path: Path) -> None:
    ledger_dir = write_ledger(snapshot, tmp_path / "ledger", [app_failed(snapshot, _at(30))])
    bad = ledger_dir / "events" / "2026" / "10" / "25" / "20261025T110000Z-deploy.failed-x.json"
    bad.write_text('{"schema": "ops.v1", "kind": "deploy.failed"}')
    (bad.parent / "broken.json").write_text("{not json")
    duplicate = app_failed(snapshot, _at(30))
    duplicate["recorded_at"] = "2026-10-25T11:59:00Z"
    (bad.parent / f"dup-{duplicate['event_id']}.json").write_text(json.dumps(duplicate))
    warnings: list[str] = []

    events = snapshot.load_events(ledger_dir, warnings.append)

    assert len(events) == 1
    assert events[0]["recorded_at"] == "2026-10-25T11:30:05Z"  # the earliest recorded copy
    assert len(warnings) == 2
    assert all("skipped" in w for w in warnings)
    docs = snapshot.build(ledger_dir, now=NOW, status=None, run_url=None, ledger_head=None)
    assert docs["meta.json"]["event_count"] == 1
