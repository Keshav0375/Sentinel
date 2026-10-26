"""Static contract of the ops emitter jobs in the Sentinel workflows (backend task 10.2).

Ledger writes happen only in dedicated `ops-start` / `ops-events` jobs with `contents: write`, gated
on the workflow's readiness gate, non-blocking, after the facts they report. No PR-triggered job
writes. The tests read the workflows as YAML; the slot check runs emit.py. Nothing here calls
GitHub.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github/workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))
EMIT = REPO_ROOT / "scripts/ops/emit.py"
OPS_EVENT = "./.github/actions/ops-event"

EMITTERS: dict[str, list[str]] = {
    "ci_backend_deployment.yml": ["ops-start", "ops-events"],
    "ci_incident_response.yml": ["ops-start", "ops-events"],
    "ci_backend_scale.yml": ["ops-events"],
    "infra_runners.yml": ["ops-start", "ops-events"],
    "infra_deploy.yml": ["ops-start", "ops-events"],
}
SHAPE_CMDS: dict[tuple[str, str], str] = {
    ("ci_backend_deployment.yml", "ops-start"): "backend-start",
    ("ci_backend_deployment.yml", "ops-events"): "backend-deploy",
    ("ci_incident_response.yml", "ops-start"): "incident-start",
    ("ci_incident_response.yml", "ops-events"): "incident-result",
    ("ci_backend_scale.yml", "ops-events"): "scale",
    ("infra_runners.yml", "ops-start"): "infra-runners-start",
    ("infra_runners.yml", "ops-events"): "infra-runners",
    ("infra_deploy.yml", "ops-start"): "infra-deploy-start",
    ("infra_deploy.yml", "ops-events"): "infra-deploy",
}
PREFLIGHT_GATE = "needs.preflight.outputs.configured == 'true'"
OIDC_GATE = "needs.oidc-ready.outputs.ready == 'true'"
CHECKOUT = "actions/checkout@"
WITH_KEYS = {"kind", "source-id", "state", "chain", "data"}
# The one non-emitter job that writes: the reconciler appends to ops-ledger (task 10.3, D12).
RECONCILER: dict[tuple[str, str], dict[str, str]] = {
    ("ops_reconcile.yml", "reconcile"): {
        "contents": "write",
        "actions": "read",
        "id-token": "write",
    },
}


def _doc(path: Path) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc


def _on(doc: dict[str, Any]) -> Any:
    # PyYAML reads the bare key `on` as the boolean True.
    return doc.get("on", doc.get(True))


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def _if(job: dict[str, Any]) -> str:
    return " ".join(str(job.get("if", "")).split())


def _gate(workflow: str) -> str:
    return OIDC_GATE if workflow.startswith("infra_") else PREFLIGHT_GATE


def _ops_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for workflow, ids in EMITTERS.items():
        jobs = _doc(WORKFLOW_DIR / workflow)["jobs"]
        out += [(workflow, job_id, jobs[job_id]) for job_id in ids]
    return out


OPS_IDS = [f"{wf}:{job_id}" for wf, job_id, _ in _ops_jobs()]


def _writes_contents(permissions: Any) -> bool:
    if permissions == "write-all":
        return True
    return isinstance(permissions, dict) and permissions.get("contents") == "write"


def _ops_event_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in job.get("steps", []) if s.get("uses") == OPS_EVENT]


def test_emitter_jobs_exact() -> None:
    found: dict[str, list[str]] = {}
    for path in WORKFLOWS:
        for job_id, job in _doc(path)["jobs"].items():
            if _ops_event_steps(job):
                found.setdefault(path.name, []).append(job_id)
    assert found == EMITTERS


def test_only_ops_jobs_write() -> None:
    ops = {(wf, job_id) for wf, job_id, _ in _ops_jobs()}
    for path in WORKFLOWS:
        doc = _doc(path)
        assert not _writes_contents(doc.get("permissions")), path.name
        for job_id, job in doc["jobs"].items():
            if _writes_contents(job.get("permissions")):
                if (path.name, job_id) in RECONCILER:
                    assert job["permissions"] == RECONCILER[(path.name, job_id)]
                    continue
                assert (path.name, job_id) in ops, f"{path.name}:{job_id}"
    for wf, job_id, job in _ops_jobs():
        assert job["permissions"] == {"contents": "write"}, f"{wf}:{job_id}"
    for wf, _ in RECONCILER:
        on = _on(_doc(WORKFLOW_DIR / wf))
        triggers = {on} if isinstance(on, str) else set(on)
        # Fork safety: a contents-write job never runs on PR-controlled triggers.
        assert not triggers & {"pull_request", "pull_request_target"}, wf


@pytest.mark.parametrize(("workflow", "job_id", "job"), _ops_jobs(), ids=OPS_IDS)
def test_ops_jobs_gated_and_non_blocking(workflow: str, job_id: str, job: dict[str, Any]) -> None:
    condition = _if(job)
    assert _gate(workflow) in condition
    gate_job = "oidc-ready" if workflow.startswith("infra_") else "preflight"
    assert gate_job in _needs(job)
    if job_id == "ops-events":
        assert condition.startswith("always() && ")
    else:
        assert "always()" not in condition
    assert job["timeout-minutes"] == 5
    steps = job["steps"]
    assert steps[0]["uses"].startswith(CHECKOUT)
    assert steps[0]["with"]["persist-credentials"] is False
    for step in steps:
        assert step.get("continue-on-error") is True, step.get("id") or step.get("name")
    dumped = yaml.safe_dump(job)
    assert "azure/login" not in dumped
    assert "secrets." not in dumped


@pytest.mark.parametrize(("workflow", "job_id", "job"), _ops_jobs(), ids=OPS_IDS)
def test_ops_event_steps_wired(workflow: str, job_id: str, job: dict[str, Any]) -> None:
    steps = _ops_event_steps(job)
    assert steps
    for step in steps:
        slot = step["id"]
        assert step["if"] == f"steps.shape.outputs.{slot}-kind != ''"
        assert set(step["with"]) == WITH_KEYS
        assert "token" not in step["with"]
        for key, value in step["with"].items():
            assert value == f"${{{{ steps.shape.outputs.{slot}-{key} }}}}"


@pytest.mark.parametrize(("workflow", "job_id", "job"), _ops_jobs(), ids=OPS_IDS)
def test_shape_steps(workflow: str, job_id: str, job: dict[str, Any]) -> None:
    (shape,) = [s for s in job["steps"] if s.get("id") == "shape"]
    cmd = SHAPE_CMDS[(workflow, job_id)]
    assert (
        shape["run"]
        == f'set -euo pipefail\npython3 scripts/ops/emit.py {cmd} >> "$GITHUB_OUTPUT"\n'
    )
    for step in job["steps"]:
        assert "${{" not in step.get("run", "")
    index = job["steps"].index(shape)
    assert all(s.get("uses") == OPS_EVENT for s in job["steps"][index + 1 :])


@pytest.mark.parametrize(("workflow", "job_id", "job"), _ops_jobs(), ids=OPS_IDS)
def test_needs_cover_reported_jobs(workflow: str, job_id: str, job: dict[str, Any]) -> None:
    read = set(re.findall(r"needs\.([a-z-]+)\.", yaml.safe_dump(job)))
    assert read <= _needs(job), read - _needs(job)


def test_reported_results() -> None:
    jobs = {(wf, job_id): job for wf, job_id, job in _ops_jobs()}

    def env(workflow: str) -> dict[str, str]:
        (shape,) = [s for s in jobs[(workflow, "ops-events")]["steps"] if s.get("id") == "shape"]
        result: dict[str, str] = shape["env"]
        return result

    assert env("ci_backend_deployment.yml") == {
        "QUALITY_RESULT": "${{ needs.run-quality.result }}",
        "BUILD_RESULT": "${{ needs.build-and-push.result }}",
        "DEPLOY_RESULT": "${{ needs.deploy-to-aks.result }}",
        "TESTS_RESULT": "${{ needs.run-tests.result }}",
    }
    assert env("ci_backend_scale.yml") == {
        "SCALE_RESULT": "${{ needs.scale.result }}",
        "DIRECTION": "${{ inputs.direction }}",
        "KEEP_WARM": "${{ vars.SENTINEL_KEEP_WARM }}",
    }
    assert env("infra_runners.yml") == {"BUILD_RESULT": "${{ needs.build-and-push.result }}"}
    assert env("infra_deploy.yml") == {
        "RUN_RESULT": "${{ needs.run.result }}",
        "VERIFY_RESULT": "${{ needs.verify.result }}",
        "GUARDS_PASSED": "${{ needs.run.outputs.guards_passed }}",
    }
    incident = env("ci_incident_response.yml")
    assert incident["PIPELINE_RESULT"] == "${{ needs.run-agent-pipeline.result }}"
    assert incident["PR_DETAILS"] == "${{ needs.fetch-pr-details.outputs.pr-details }}"
    assert jobs[("ci_backend_scale.yml", "ops-events")]["environment"] == "sentinel-dev"


def test_pr_triggered_workflows_never_write() -> None:
    checked = set()
    for path in WORKFLOWS:
        doc = _doc(path)
        on = _on(doc)
        triggers = {on} if isinstance(on, str) else set(on)
        if not triggers & {"pull_request", "pull_request_target"}:
            continue
        checked.add(path.name)
        assert not _writes_contents(doc.get("permissions")), path.name
        for job_id, job in doc["jobs"].items():
            assert not _ops_event_steps(job), f"{path.name}:{job_id}"
            assert not _writes_contents(job.get("permissions")), f"{path.name}:{job_id}"
    assert checked >= {"ci.yml", "ci_validation.yml", "infra_validate_plan.yml"}


def test_infra_emitters() -> None:
    for name in ("infra_pause.yml", "infra_validate_plan.yml"):
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        assert OPS_EVENT not in text, name
        assert "contents: write" not in text, name
    for workflow, job_id, job in _ops_jobs():
        if not workflow.startswith("infra_"):
            continue
        assert OIDC_GATE in _if(job), f"{workflow}:{job_id}"
        if workflow == "infra_deploy.yml":
            assert "inputs.action == 'apply'" in _if(job), job_id
        assert job["defaults"]["run"]["working-directory"] == "infra"
        assert "environment" not in job
        (shape,) = [s for s in job["steps"] if s.get("id") == "shape"]
        assert shape["working-directory"] == "."


def _printed_slots(cmd: str) -> set[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_REPOSITORY": "Keshav0375/Sentinel",
        "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(EMIT), cmd],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return {
        line.partition("=")[0].removesuffix("-kind")
        for line in proc.stdout.splitlines()
        if line.partition("=")[0].endswith("-kind")
    }


@pytest.mark.parametrize(("workflow", "job_id", "job"), _ops_jobs(), ids=OPS_IDS)
def test_emit_slots_match_workflow(workflow: str, job_id: str, job: dict[str, Any]) -> None:
    slots = {s["id"] for s in _ops_event_steps(job)}
    assert slots == _printed_slots(SHAPE_CMDS[(workflow, job_id)])
