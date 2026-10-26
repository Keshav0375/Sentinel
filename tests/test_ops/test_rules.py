"""scripts/ops/rules.py: the event derivation shared by emit.py and the reconciler (task 10.2).

rules.py is a script beside ledger.py, so it is loaded with importlib with scripts/ops on sys.path
(it imports `ledger` as a sibling). Nothing here touches git or the network.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts/ops"
WORKFLOW_DIR = REPO_ROOT / ".github/workflows"

SHA = "4f1d2a9c0b8e7d6f5a4b3c2d1e0f9a8b7c6d5e4f"
RUN_ID = 123
ATTEMPT = 2
CORRELATION = "6f9619ff-8b86-4011-b42d-00cf4fc964ff"
INCIDENT = "0b6c3d2e-1f4a-4b5c-8d9e-0a1b2c3d4e5f"
RUN = {"sha": SHA, "run_id": RUN_ID, "run_attempt": ATTEMPT}
BACKEND_SOURCE = "gha:Keshav0375/Sentinel/ci_backend_deployment.yml"
RUN_URL = "https://github.com/Keshav0375/Sentinel/actions/runs/123"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: the frozen dataclasses resolve their postponed annotations here.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def rules() -> ModuleType:
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    return _load("ops_rules", OPS_DIR / "rules.py")


@pytest.fixture(scope="session")
def emit() -> ModuleType:
    return _load("ops_emit", OPS_DIR / "emit.py")


@pytest.fixture(scope="session")
def incident_dispatch() -> ModuleType:
    return _load("ops_incident_dispatch", REPO_ROOT / "scripts/incident_dispatch.py")


def _jobs(rules: ModuleType, q: str, b: str, d: str, t: str) -> dict[str, Any]:
    return {
        "run-quality": rules.JobFact(q),
        "build-and-push": rules.JobFact(b),
        "deploy-to-aks": rules.JobFact(d),
        "run-tests": rules.JobFact(t),
    }


def _shape(specs: list[Any]) -> list[tuple[str, str | None]]:
    return [(s.kind, s.data.get("failed_stage")) for s in specs]


def _all_specs(rules: ModuleType) -> Iterator[Any]:
    """One of every spec the rules can produce, for the build-validation test."""
    for q, b, d, t in (
        ("success", "success", "success", "success"),
        ("failure", "skipped", "skipped", "skipped"),
        ("success", "failure", "skipped", "skipped"),
        ("success", "success", "failure", "skipped"),
        ("success", "success", "success", "failure"),
    ):
        yield from rules.backend_deploy_events(_jobs(rules, q, b, d, t), **RUN)
    yield from rules.backend_deploy_events(_timed_jobs(rules), **RUN, with_started=True)
    yield rules.backend_build_started(**RUN)
    yield from rules.incident_start_events(
        correlation_id=CORRELATION,
        signal_type="runtime_error",
        service="sentinel-watchtower",
        severity="P2",
        run_id=RUN_ID,
    )
    for pipeline, resolution in (("success", "rollback"), ("success", "escalated")):
        yield from _result(rules, pipeline_result=pipeline, resolution_type=resolution)
    yield from _result(rules, ensure_result="failure", pipeline_result="skipped")
    yield from _result(rules, pipeline_result="failure")
    for event_name, direction, keep_warm in (
        ("schedule", "", None),
        ("workflow_dispatch", "up", False),
        ("workflow_dispatch", "down", False),
    ):
        yield from rules.scale_events(
            event_name=event_name,
            direction=direction,
            result="success",
            keep_warm=keep_warm,
            run_id=RUN_ID,
            run_attempt=ATTEMPT,
        )
    yield from rules.infra_build_events(_timed(rules, "failure"), **RUN, with_started=True)
    yield from rules.infra_deploy_events(
        _timed(rules, "success"), _timed(rules, "success"), guards_passed=True, **RUN
    )


def _timed(rules: ModuleType, result: str) -> Any:
    return rules.JobFact(result, "2026-10-25T12:00:00Z", "2026-10-25T12:01:30Z")


def _timed_jobs(rules: ModuleType) -> dict[str, Any]:
    return {
        "run-quality": rules.JobFact("success", "2026-10-25T12:00:00Z", "2026-10-25T12:03:00Z"),
        "build-and-push": rules.JobFact("success", "2026-10-25T12:03:05Z", "2026-10-25T12:10:00Z"),
        "deploy-to-aks": rules.JobFact("success", "2026-10-25T12:10:05Z", "2026-10-25T12:20:00Z"),
        "run-tests": rules.JobFact("failure", "2026-10-25T12:20:05Z", "2026-10-25T12:30:05Z"),
    }


def _result(rules: ModuleType, **overrides: Any) -> list[Any]:
    args: dict[str, Any] = {
        "correlation_id": CORRELATION,
        "ensure_result": "success",
        "pipeline_result": "success",
        "resolution_type": "rollback",
        "confidence": "0.86",
        "root_cause": "bad config in deploy 4f1d2a9",
        "evidence": "",
        "incident_id": INCIDENT,
        "pr_details": None,
        "run_id": RUN_ID,
    }
    args.update(overrides)
    result: list[Any] = rules.incident_result_events(**args)
    return result


# ── Backend deployment ────────────────────────────────────────────────────────


def test_backend_unconfigured_emits_nothing(rules: ModuleType) -> None:
    skipped = _jobs(rules, "skipped", "skipped", "skipped", "skipped")
    assert rules.backend_deploy_events(skipped, **RUN) == []
    assert rules.backend_deploy_events(skipped, **RUN, with_started=True) == []
    assert rules.backend_deploy_events({}, **RUN) == []


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        (
            ("success", "success", "success", "success"),
            [("build.succeeded", None), ("deploy.succeeded", None)],
        ),
        (("failure", "skipped", "skipped", "skipped"), [("build.failed", "quality")]),
        (("cancelled", "skipped", "skipped", "skipped"), [("build.failed", "quality")]),
        (("success", "failure", "skipped", "skipped"), [("build.failed", "build")]),
        (
            ("success", "success", "failure", "skipped"),
            [("build.succeeded", None), ("deploy.failed", "deploy")],
        ),
        (
            ("success", "success", "success", "failure"),
            [("build.succeeded", None), ("deploy.failed", "test")],
        ),
        (("success", "success", "success", ""), [("build.succeeded", None)]),
    ],
)
def test_backend_matrix(
    rules: ModuleType, results: tuple[str, str, str, str], expected: list[tuple[str, str | None]]
) -> None:
    assert _shape(rules.backend_deploy_events(_jobs(rules, *results), **RUN)) == expected


def test_backend_ids_match_recipe(rules: ModuleType, ledger: ModuleType) -> None:
    specs = rules.backend_deploy_events(
        _jobs(rules, "success", "success", "success", "success"), **RUN
    )
    deploy = specs[-1]
    assert deploy.kind == "deploy.succeeded"
    assert deploy.source == BACKEND_SOURCE
    assert deploy.source_id == "123.2"
    assert deploy.state == ""
    assert deploy.event_id == ledger.compute_event_id(
        "deploy.succeeded", BACKEND_SOURCE, "123.2", ""
    )


def test_backend_chain_and_data(rules: ModuleType) -> None:
    specs = rules.backend_deploy_events(
        _jobs(rules, "success", "success", "success", "success"), **RUN
    )
    for spec in specs:
        assert spec.chain == {"sha": SHA, "run_id": RUN_ID}
        assert spec.data == {"version": "sha-4f1d2a9", "run_url": RUN_URL}
        assert "failed_stage" not in spec.data
        assert spec.ts is None
    failed = rules.backend_deploy_events(
        _jobs(rules, "success", "failure", "skipped", "skipped"), **RUN
    )
    assert failed[0].data == {"version": "sha-4f1d2a9", "run_url": RUN_URL, "failed_stage": "build"}


def test_backend_with_started_and_times(rules: ModuleType) -> None:
    specs = rules.backend_deploy_events(_timed_jobs(rules), **RUN, with_started=True)
    by_kind = {s.kind: s for s in specs}
    assert [s.kind for s in specs] == [
        "build.started",
        "build.succeeded",
        "deploy.started",
        "deploy.failed",
    ]
    assert by_kind["build.started"].ts == "2026-10-25T12:00:00Z"
    assert by_kind["build.succeeded"].ts == "2026-10-25T12:10:00Z"
    assert by_kind["build.succeeded"].data["duration_s"] == 600.0
    assert by_kind["deploy.started"].ts == "2026-10-25T12:10:05Z"
    assert by_kind["deploy.failed"].ts == "2026-10-25T12:30:05Z"
    assert by_kind["deploy.failed"].data["duration_s"] == 1200.0
    assert by_kind["deploy.failed"].data["failed_stage"] == "test"
    assert "duration_s" not in by_kind["build.started"].data

    jobs = _timed_jobs(rules)
    jobs["build-and-push"] = rules.JobFact(
        "failure", "2026-10-25T12:03:05Z", "2026-10-25T12:05:00Z"
    )
    jobs["deploy-to-aks"] = rules.JobFact("skipped")
    kinds = [s.kind for s in rules.backend_deploy_events(jobs, **RUN, with_started=True)]
    assert kinds == ["build.started", "build.failed"]


def test_seconds_between(rules: ModuleType) -> None:
    assert rules.seconds_between("2026-10-25T12:00:00Z", "2026-10-25T12:00:09.6Z") == 10.0
    assert rules.seconds_between("2026-10-25T12:00:00+00:00", "2026-10-25T12:01:00Z") == 60.0
    assert rules.seconds_between("2026-10-25T12:01:00Z", "2026-10-25T12:00:00Z") is None
    assert rules.seconds_between(None, "2026-10-25T12:00:00Z") is None
    assert rules.seconds_between("not a time", "2026-10-25T12:00:00Z") is None


def test_build_started_matches_with_started_variant(rules: ModuleType) -> None:
    started = rules.backend_deploy_events(_timed_jobs(rules), **RUN, with_started=True)[0]
    assert started.kind == "build.started"
    assert rules.backend_build_started(**RUN).event_id == started.event_id


def test_every_spec_builds(rules: ModuleType, ledger: ModuleType) -> None:
    specs = list(_all_specs(rules))
    assert len(specs) > 20
    schema = ledger.load_schema()
    for spec in specs:
        event = spec.build()
        assert ledger.validate(event, schema) == [], spec.kind
        assert event["event_id"] == spec.event_id
        assert event["chain"] == spec.chain


# ── Incident response ─────────────────────────────────────────────────────────


def test_incident_start(rules: ModuleType) -> None:
    def start(**overrides: Any) -> list[Any]:
        args: dict[str, Any] = {
            "correlation_id": CORRELATION,
            "signal_type": "runtime_error",
            "service": "sentinel-watchtower",
            "severity": "P2",
            "run_id": RUN_ID,
        }
        args.update(overrides)
        result: list[Any] = rules.incident_start_events(**args)
        return result

    alert, agent = start()
    assert (alert.kind, alert.state, alert.source_id) == (
        "alert.received",
        "runtime_error",
        CORRELATION,
    )
    assert alert.data == {
        "signal_type": "runtime_error",
        "service": "sentinel-watchtower",
        "severity": "P2",
    }
    assert alert.chain == {"correlation_id": CORRELATION}
    assert (agent.kind, agent.state, agent.data) == ("agent.started", "", {"run_url": RUN_URL})
    assert agent.chain == {"correlation_id": CORRELATION}

    assert [s.kind for s in start(signal_type="other")] == ["agent.started"]
    assert [s.kind for s in start(signal_type=None)] == ["agent.started"]
    assert start(service="Bad_Name")[0].data["service"] == "unknown"
    assert start(service=None)[0].data["service"] == "unknown"
    assert "severity" not in start(severity="x" * 40)[0].data
    assert rules.SIGNAL_TYPES == ("deploy_failure", "runtime_error")


def test_signal_types_match_incident_dispatch(
    rules: ModuleType, incident_dispatch: ModuleType
) -> None:
    assert rules.SIGNAL_TYPES == incident_dispatch.SIGNAL_TYPES


def test_incident_result_matrix(rules: ModuleType) -> None:
    rollback = _result(rules)
    assert [(s.kind, s.state) for s in rollback] == [("agent.decided", "rollback")]
    assert rollback[0].source_id == CORRELATION
    assert rollback[0].data == {
        "resolution_type": "rollback",
        "confidence": 0.86,
        "root_cause_summary": "bad config in deploy 4f1d2a9",
        "run_url": RUN_URL,
    }

    escalated = _result(rules, resolution_type="escalated", root_cause="", evidence="no deploy")
    assert [(s.kind, s.state) for s in escalated] == [
        ("agent.decided", "escalated"),
        ("escalation.raised", ""),
    ]
    assert "root_cause_summary" not in escalated[0].data
    assert escalated[1].data == {"summary": "no deploy", "run_url": RUN_URL}
    assert _result(rules, resolution_type="weird")[0].state == "escalated"

    def failed(**overrides: Any) -> list[tuple[str, str]]:
        return [(s.kind, s.data["error_class"]) for s in _result(rules, **overrides)]

    assert failed(ensure_result="failure", pipeline_result="skipped") == [
        ("agent.failed", "backend_unavailable")
    ]
    assert failed(ensure_result="cancelled", pipeline_result="skipped") == [
        ("agent.failed", "cancelled")
    ]
    assert failed(pipeline_result="failure") == [("agent.failed", "pipeline_fault")]
    assert failed(pipeline_result="cancelled") == [("agent.failed", "cancelled")]
    assert _result(rules, pipeline_result="skipped") == []


def test_incident_result_chain(rules: ModuleType) -> None:
    linked = _result(rules, pr_details={"sha": SHA, "branch": "runtime/04", "number": 7})
    assert linked[0].chain == {
        "correlation_id": CORRELATION,
        "incident_id": INCIDENT,
        "sha": SHA,
        "scenario": "runtime/04",
    }
    plain = _result(rules, pr_details={"sha": SHA[:7], "branch": "main"}, incident_id="nope")
    assert plain[0].chain == {"correlation_id": CORRELATION}


@pytest.mark.parametrize(
    ("raw", "expected"), [("0.86", 0.86), ("1.7", 1.0), ("-2", 0.0), ("x", 0.0), ("nan", 0.0)]
)
def test_confidence_parsing(rules: ModuleType, raw: str, expected: float) -> None:
    assert _result(rules, confidence=raw)[0].data["confidence"] == expected


def test_secret_text_dropped(rules: ModuleType) -> None:
    warnings: list[str] = []
    leaky = "the pod at " + "10.0." + "0.12 refused connections"
    specs = _result(rules, root_cause=leaky, warn=warnings.append)
    assert [s.kind for s in specs] == ["agent.decided"]
    assert "root_cause_summary" not in specs[0].data
    assert len(warnings) == 1
    assert "root_cause_summary" in warnings[0]
    assert "10.0." not in warnings[0]
    specs[0].build()

    escalated = _result(rules, resolution_type="escalated", root_cause=leaky, warn=warnings.append)
    assert escalated[1].data == {"run_url": RUN_URL}


# ── Scale ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("event_name", "direction", "result", "keep_warm", "replicas"),
    [
        ("schedule", "", "success", None, 0),
        ("schedule", "", "success", True, 0),
        ("workflow_dispatch", "up", "success", False, 1),
        ("workflow_dispatch", "down", "success", False, 0),
        ("workflow_dispatch", "down", "success", True, None),
        ("workflow_dispatch", "down", "success", None, None),
        ("schedule", "", "failure", False, None),
        ("workflow_dispatch", "up", "skipped", False, None),
    ],
)
def test_scale_matrix(
    rules: ModuleType,
    event_name: str,
    direction: str,
    result: str,
    keep_warm: bool | None,
    replicas: int | None,
) -> None:
    specs = rules.scale_events(
        event_name=event_name,
        direction=direction,
        result=result,
        keep_warm=keep_warm,
        run_id=RUN_ID,
        run_attempt=ATTEMPT,
    )
    if replicas is None:
        assert specs == []
        return
    (spec,) = specs
    assert spec.kind == "scale.changed"
    assert spec.state == str(replicas)
    assert spec.source_id == "123.2"
    assert spec.source == "gha:Keshav0375/Sentinel/ci_backend_scale.yml"
    assert spec.chain == {"run_id": RUN_ID}
    assert spec.data["replicas"] == replicas
    assert spec.data["run_url"] == RUN_URL
    assert spec.data.get("keep_warm") == keep_warm


def test_parse_bool(rules: ModuleType) -> None:
    assert rules.parse_bool("true") is True
    assert rules.parse_bool("false") is False
    assert rules.parse_bool("") is False
    assert rules.parse_bool(None) is None


def test_backend_replicas_pinned(rules: ModuleType) -> None:
    docs = list(yaml.safe_load_all((REPO_ROOT / "azure/k8s/deployment.yaml").read_text()))
    (deployment,) = [d for d in docs if isinstance(d, dict) and d.get("kind") == "Deployment"]
    assert deployment["spec"]["replicas"] == rules.BACKEND_REPLICAS


def _workflow(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    return doc


def test_job_names_match_workflow(rules: ModuleType) -> None:
    jobs = _workflow(rules.BACKEND_DEPLOY_WF)["jobs"]
    names = {
        job["name"]: key for key, job in jobs.items() if key in rules.BACKEND_DEPLOY_JOBS.values()
    }
    assert names == rules.BACKEND_DEPLOY_JOBS
    scale_steps = {
        s.get("id"): s.get("name") for s in _workflow(rules.SCALE_WF)["jobs"]["scale"]["steps"]
    }
    for step_id, name in rules.SCALE_STEPS.items():
        assert scale_steps[step_id] == name


def test_correlation_uuid_shared(emit: ModuleType, incident_dispatch: ModuleType) -> None:
    for raw in ("6F9619FF-8B86-4011-B42D-00CF4FC964FF", CORRELATION, "evt-123"):
        assert emit.correlation_uuid(raw) == incident_dispatch.correlation_uuid(raw)
    assert emit.correlation_uuid(None) is None
    assert emit.correlation_uuid("  ") is None


# ── Infra ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("success", [("build.succeeded", None)]),
        ("failure", [("build.failed", "build")]),
        ("cancelled", [("build.failed", "build")]),
        ("skipped", []),
        ("", []),
    ],
)
def test_infra_build_matrix(
    rules: ModuleType, result: str, expected: list[tuple[str, str | None]]
) -> None:
    assert _shape(rules.infra_build_events(rules.JobFact(result), **RUN)) == expected


@pytest.mark.parametrize(
    ("run", "verify", "guards", "expected"),
    [
        ("failure", "skipped", False, [("deploy.failed", "guards")]),
        ("failure", "success", True, [("deploy.failed", "apply")]),
        ("cancelled", "skipped", True, [("deploy.failed", "apply")]),
        ("success", "success", True, [("deploy.succeeded", None)]),
        ("success", "failure", True, [("deploy.failed", "verify")]),
        ("success", "skipped", True, []),
        ("skipped", "skipped", False, []),
    ],
)
def test_infra_deploy_matrix(
    rules: ModuleType,
    run: str,
    verify: str,
    guards: bool,
    expected: list[tuple[str, str | None]],
) -> None:
    specs = rules.infra_deploy_events(
        rules.JobFact(run), rules.JobFact(verify), guards_passed=guards, **RUN
    )
    assert _shape(specs) == expected


def test_infra_times(rules: ModuleType) -> None:
    build = rules.infra_build_events(_timed(rules, "success"), **RUN, with_started=True)
    assert [(s.kind, s.ts) for s in build] == [
        ("build.started", "2026-10-25T12:00:00Z"),
        ("build.succeeded", "2026-10-25T12:01:30Z"),
    ]
    assert build[1].data["duration_s"] == 90.0
    run = rules.JobFact("success", "2026-10-25T12:00:00Z", "2026-10-25T12:10:00Z")
    verify = rules.JobFact("failure", "2026-10-25T12:10:05Z", "2026-10-25T12:12:00Z")
    deploy = rules.infra_deploy_events(run, verify, guards_passed=True, **RUN, with_started=True)
    assert [(s.kind, s.ts) for s in deploy] == [
        ("deploy.started", "2026-10-25T12:00:00Z"),
        ("deploy.failed", "2026-10-25T12:12:00Z"),
    ]
    assert deploy[1].data["duration_s"] == 720.0


def test_infra_chain_is_run_id_only(rules: ModuleType) -> None:
    (deploy,) = rules.infra_deploy_events(
        rules.JobFact("success"), rules.JobFact("success"), guards_passed=True, **RUN
    )
    assert deploy.chain == {"run_id": RUN_ID}
    assert deploy.source.endswith("/infra_deploy.yml")
    assert deploy.data == {"version": "sha-4f1d2a9", "run_url": RUN_URL}
    (build,) = rules.infra_build_events(rules.JobFact("success"), **RUN)
    assert build.chain == {"run_id": RUN_ID}
    assert build.source == "gha:Keshav0375/Sentinel/infra_runners.yml"


def test_infra_started_match_with_started_variants(rules: ModuleType) -> None:
    build = rules.infra_build_events(_timed(rules, "success"), **RUN, with_started=True)[0]
    assert rules.infra_build_started(**RUN).event_id == build.event_id
    run = _timed(rules, "success")
    deploy = rules.infra_deploy_events(
        run, rules.JobFact("success"), guards_passed=True, **RUN, with_started=True
    )[0]
    assert deploy.kind == "deploy.started"
    assert rules.infra_deploy_started(**RUN).event_id == deploy.event_id


def test_infra_names_match_workflows(rules: ModuleType) -> None:
    runners = _workflow(rules.INFRA_RUNNERS_WF)["jobs"]
    names = {
        job["name"]: key for key, job in runners.items() if key in rules.INFRA_RUNNERS_JOBS.values()
    }
    assert names == rules.INFRA_RUNNERS_JOBS
    deploy = _workflow(rules.INFRA_DEPLOY_WF)["jobs"]
    run_name = deploy["run"]["name"]
    assert run_name.startswith("${{ inputs.action }} · ")
    assert run_name.replace("${{ inputs.action }}", "apply").startswith(
        rules.INFRA_DEPLOY_RUN_JOB_PREFIX
    )
    assert deploy["verify"]["name"] == rules.INFRA_DEPLOY_VERIFY_JOB
    assert rules.INFRA_GUARDS_STEP in [s.get("name") for s in deploy["run"]["steps"]]
