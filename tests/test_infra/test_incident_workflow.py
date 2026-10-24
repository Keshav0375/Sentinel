"""Static contract checks for .github/workflows/ci_incident_response.yml (backend task 8.3).

Nothing here calls GitHub, Azure or the backend: the workflow is parsed and linted offline
(actionlint and yamllint skip when not installed).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci_incident_response.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
WF: dict[Any, Any] = yaml.safe_load(TEXT)
JOBS: dict[str, dict[str, Any]] = WF["jobs"]

# Same config as tests/test_infra/test_composite_actions.py (copied, not imported).
YAMLLINT_CONFIG = """\
extends: default
rules:
  truthy:
    check-keys: false
  line-length:
    max: 120
  document-start: disable
"""

VARS_CONTRACT = {
    "AZURE_CLIENT_ID",
    "AZURE_TENANT_ID",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_IDENTITY_TENANT_ID",
    "AZURE_GHA_CLIENT_ID",
    "SENTINEL_API_TENANT_ID",
    "SENTINEL_API_AUDIENCE",
    "AKS_RESOURCE_GROUP",
    "AKS_CLUSTER_NAME",
    "ACR_NAME",
    "ACR_LOGIN_SERVER",
    "KEY_VAULT_URI",
    "PG_HOST",
    "PG_DATABASE",
    "PG_USER_BACKEND",
    "PG_USER_GHA",
    "SENTINEL_KEEP_WARM",
}
LOGIN_ACTIONS = (
    "get-kv-secrets",
    "get-db-token",
    "backend-up",
    "backend-down",
    "psql-exec",
)


def _need(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not on PATH")


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def _steps(job_id: str) -> list[dict[str, Any]]:
    return JOBS[job_id]["steps"]


def _uses(step: dict[str, Any]) -> str:
    return str(step.get("uses", ""))


def _is_login(step: dict[str, Any]) -> bool:
    return _uses(step).startswith("azure/login@")


def _action(step: dict[str, Any]) -> str:
    uses = _uses(step)
    return uses.removeprefix("./.github/actions/") if uses.startswith("./.github/actions/") else ""


def _find(job_id: str, action: str) -> dict[str, Any]:
    return next(s for s in _steps(job_id) if _action(s) == action)


def test_header() -> None:
    on = WF.get("on", WF.get(True))

    assert WF["name"] == "[sentinel] incident — response pipeline"
    assert on == {"repository_dispatch": {"types": ["incident-alert"]}}
    assert WF["permissions"] == {"id-token": "write", "contents": "read"}
    assert WF["concurrency"] == {"group": "sentinel-backend", "cancel-in-progress": False}


def test_dag_needs_graph() -> None:
    expected = {
        "ensure-backend-up": set(),
        "fetch-service-info": {"ensure-backend-up"},
        "fetch-pr-details": {"ensure-backend-up"},
        "fetch-datadog-logs": {"ensure-backend-up"},
        "run-agent-pipeline": {
            "ensure-backend-up",
            "fetch-service-info",
            "fetch-pr-details",
            "fetch-datadog-logs",
        },
        "generate-pr-content": {"ensure-backend-up", "run-agent-pipeline"},
        "create-rollback-pr": {"run-agent-pipeline", "generate-pr-content"},
        "notify-rollback": {"run-agent-pipeline", "create-rollback-pr"},
        "notify-escalation": {"run-agent-pipeline"},
        "teardown-backend": {
            "ensure-backend-up",
            "run-agent-pipeline",
            "notify-rollback",
            "notify-escalation",
        },
        "summary": {
            "ensure-backend-up",
            "run-agent-pipeline",
            "create-rollback-pr",
            "teardown-backend",
        },
    }

    assert {job_id: _needs(job) for job_id, job in JOBS.items()} == expected


def test_branches_are_exclusive() -> None:
    rollback = JOBS["generate-pr-content"]["if"]
    escalation = JOBS["notify-escalation"]["if"]

    assert "resolution-type == 'rollback'" in rollback
    assert "resolution-type == 'escalated'" in escalation
    for condition in (rollback, escalation):
        assert "needs.run-agent-pipeline.result == 'success'" in condition
    assert "needs.generate-pr-content.result == 'success'" in JOBS["create-rollback-pr"]["if"]
    assert "needs.create-rollback-pr.result == 'success'" in JOBS["notify-rollback"]["if"]


def test_teardown_and_summary_always_run() -> None:
    assert JOBS["teardown-backend"]["if"] == "always()"
    assert JOBS["summary"]["if"] == "always()"
    down = _find("teardown-backend", "backend-down")
    assert down["with"]["keep-warm"] == "${{ vars.SENTINEL_KEEP_WARM }}"


def test_run_agent_pipeline_polls_and_degrades() -> None:
    job = JOBS["run-agent-pipeline"]
    run_text = "\n".join(s.get("run", "") for s in job["steps"])

    assert job["env"]["POLL_INTERVAL_SECONDS"] == "15"
    assert job["env"]["POLL_TIMEOUT_SECONDS"] == "600"
    assert "/webhooks/incident" in run_text
    assert "incident_dispatch.py result" in run_text
    assert "needs.ensure-backend-up.result == 'success'" in job["if"]


def test_ensure_backend_up_alerts_teams_on_failure() -> None:
    job = JOBS["ensure-backend-up"]
    notify = [s for s in job["steps"] if _action(s) == "notify-teams"]

    assert len(notify) == 1
    assert notify[0]["if"].startswith("failure()")
    assert "NOT processed" in notify[0]["with"]["title"]
    assert "backend-url" in job["outputs"]


@pytest.mark.parametrize(
    "pattern",
    [
        r"gh\s+pr\s+merge",
        r"--auto\b",
        r"gh\s+pr\s+review",
        r"--approve",
        r"--force",
        r"pull_request_target",
        r"GH_PAT",
        r"git\s+push[^\n]*\bmain\b",
    ],
)
def test_forbidden_commands_absent(pattern: str) -> None:
    assert re.search(pattern, TEXT) is None


def test_revert_pr_pat_only_in_create_rollback_pr() -> None:
    holders = {job_id for job_id, job in JOBS.items() if "REVERT_PR_PAT" in yaml.safe_dump(job)}

    assert holders == {"create-rollback-pr"}
    checkout = next(
        s
        for s in _steps("create-rollback-pr")
        if s.get("with", {}).get("repository") == "Keshav0375/Sentinel-deployment"
    )
    assert checkout["with"]["token"] == "${{ secrets.REVERT_PR_PAT }}"


def test_secrets_and_vars_are_the_contract_names() -> None:
    assert set(re.findall(r"secrets\.([A-Za-z_]+)", TEXT)) == {"REVERT_PR_PAT"}
    used = set(re.findall(r"vars\.([A-Z_]+)", TEXT))
    assert used <= VARS_CONTRACT, used - VARS_CONTRACT
    for stale in ("sentinel-kv-0375", "sentinel-gha", "api://sentinel-backend"):
        assert stale not in TEXT


def test_no_secret_crosses_a_job_output() -> None:
    pattern = re.compile(r"bearer|pg-token|api-key|app-key|webhook-url|secrets\.")
    for job_id, job in JOBS.items():
        for name, value in job.get("outputs", {}).items():
            assert not pattern.search(f"{name} {value}"), (job_id, name)


def test_no_expression_in_run_scripts() -> None:
    for job_id, job in JOBS.items():
        for step in job["steps"]:
            if "run" in step:
                assert "${{" not in step["run"], (job_id, step.get("name"))
                assert step["run"].startswith("set -euo pipefail"), (job_id, step.get("name"))


def test_each_job_logs_in_before_its_actions() -> None:
    for job_id, job in JOBS.items():
        steps = job["steps"]
        uses_local = any(_uses(s).startswith("./.github/actions/") for s in steps)
        if uses_local:
            assert _uses(steps[0]).startswith("actions/checkout@"), job_id
        for index, step in enumerate(steps):
            earlier = steps[:index]
            if _action(step) in LOGIN_ACTIONS:
                assert any(_is_login(s) for s in earlier), (job_id, _action(step))
            if _action(step) == "get-backend-token":
                assert any(
                    _is_login(s) and s["with"]["client-id"] == "${{ vars.AZURE_GHA_CLIENT_ID }}"
                    for s in earlier
                ), job_id


def test_environment_on_every_azure_job() -> None:
    for job_id, job in JOBS.items():
        assert job["runs-on"] == "ubuntu-latest", job_id
        needs_environment = any(_is_login(s) for s in job["steps"]) or "vars." in yaml.safe_dump(
            job
        )
        if job_id == "fetch-pr-details":
            assert "environment" not in job
            assert not needs_environment
        elif needs_environment:
            assert job["environment"] == "sentinel-dev", job_id


def test_rollback_pr_targets_the_deployment_repo() -> None:
    steps = _steps("create-rollback-pr")
    run_text = "\n".join(s.get("run", "") for s in steps)
    record = _find("create-rollback-pr", "psql-exec")

    assert any(
        s.get("with", {}).get("repository") == "Keshav0375/Sentinel-deployment" for s in steps
    )
    for fragment in ("git revert --no-edit -m 1", "gh pr create", "--base main", "--body-file"):
        assert fragment in run_text
    assert record["with"]["sql"].startswith("UPDATE incidents SET pr_number")
    assert record["with"]["pg-user"] == "${{ vars.PG_USER_GHA }}"


def test_fetch_pr_details_uses_the_deployment_repo() -> None:
    run_text = "\n".join(s.get("run", "") for s in _steps("fetch-pr-details"))

    assert "repos/${DEPLOYMENT_REPO}/pulls/" in run_text
    assert WF["env"]["DEPLOYMENT_REPO"] == "Keshav0375/Sentinel-deployment"
    assert WF["env"]["DD_SITE"] == "us5.datadoghq.com"


def test_actionlint() -> None:
    _need("actionlint")
    proc = subprocess.run(
        ["actionlint", "-no-color", str(WORKFLOW.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_yamllint() -> None:
    _need("yamllint")
    proc = subprocess.run(
        ["yamllint", "-s", "-d", YAMLLINT_CONFIG, str(WORKFLOW.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
