"""Static contract checks for .github/workflows/ci_incident_response.yml (backend task 8.3).

Nothing here calls GitHub, Azure or the backend: the workflow is parsed and linted offline
(actionlint and yamllint skip when not installed).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import uuid
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
    "resolve-stable-tag",
)
TOKEN_WITH = {
    "audience": "${{ vars.SENTINEL_API_AUDIENCE }}",
    "tenant": "${{ vars.AZURE_IDENTITY_TENANT_ID }}",
}
PAT = "${{ secrets.REVERT_PR_PAT }}"
PR_URL_SET = "needs.create-rollback-pr.outputs.pr-url != ''"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "{name} $*" >> "$STUB_LOG"\n{body}\n')
    path.chmod(0o755)


def _run_script(
    job: str,
    step_id: str,
    env: dict[str, str],
    stubs: dict[str, str],
    tmp_path: Path,
    cwd: Path = REPO_ROOT,
) -> tuple[int, str, dict[str, str], list[str]]:
    """Run one `run` step of the workflow under local bash with stubbed tools."""
    script = next(s for s in _steps(job) if s.get("id") == step_id)["run"]
    bin_dir = tmp_path / "bin"
    for name, body in {"sleep": "exit 0", **stubs}.items():
        _stub(bin_dir, name, body)
    log = tmp_path / "stub.log"
    log.write_text("")
    output = tmp_path / "github_output"
    output.write_text("")
    full_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_LOG": str(log),
        "GITHUB_OUTPUT": str(output),
        "RUNNER_TEMP": str(tmp_path),
        **env,
    }
    proc = subprocess.run(
        ["bash", "-c", script],
        env=full_env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    outputs: dict[str, str] = {}
    for line in output.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            outputs[key] = value
    return proc.returncode, proc.stdout + proc.stderr, outputs, log.read_text().splitlines()


def _create_step() -> dict[str, Any]:
    return next(s for s in _steps("create-rollback-pr") if s.get("id") == "create")


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
        "record-rollback-pr": {"run-agent-pipeline", "create-rollback-pr"},
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
            "record-rollback-pr",
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
    assert PR_URL_SET in JOBS["notify-rollback"]["if"]
    assert PR_URL_SET in JOBS["record-rollback-pr"]["if"]


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
    assert job["env"]["POLL_NOT_FOUND_GRACE_SECONDS"] == "60"
    assert "/webhooks/incident" in run_text
    assert "incident_dispatch.py result" in run_text
    assert "needs.ensure-backend-up.result == 'success'" in job["if"]


def test_backend_up_failure_pings_teams_once() -> None:
    job = JOBS["ensure-backend-up"]
    actions = [_action(s) for s in job["steps"]]
    notify = [s for s in _steps("summary") if _action(s) == "notify-teams"]

    assert "notify-teams" not in actions
    assert "get-kv-secrets" not in actions
    assert len(notify) == 1
    assert notify[0]["if"] == "${{ !cancelled() }}"
    assert "correlation_id:" in notify[0]["with"]["body"]
    assert "signal_type:" in notify[0]["with"]["body"]
    assert "backend-url" in job["outputs"]


def test_summary_teams_step_never_blank() -> None:
    step = next(s for s in _steps("summary") if s.get("id") == "notify")
    with_ = step["with"]
    title = " ".join(with_["title"].split())

    assert step["if"] == "${{ !cancelled() }}"
    assert "steps.outcome.outcome == 'success'" in with_["severity"]
    assert with_["severity"].rstrip("}} ").endswith("|| 'critical'")
    assert "steps.outcome.outcome == 'success'" in title
    assert "workflow fault" in title
    assert "incident-id || 'unknown'" in title
    assert "||" in with_["body"].splitlines()[0]


def test_ensure_backend_up_uses_shared_stable_lookup() -> None:
    step = next(s for s in _steps("ensure-backend-up") if s.get("id") == "tag")

    assert _action(step) == "resolve-stable-tag"
    assert step["with"] == {"acr-name": "${{ vars.ACR_NAME }}"}
    assert "list-metadata" not in TEXT
    assert "show-tags" not in TEXT


def test_backend_token_callers_identical() -> None:
    callers = {
        job_id: [s for s in job["steps"] if _action(s) == "get-backend-token"]
        for job_id, job in JOBS.items()
    }
    found = {job_id: steps for job_id, steps in callers.items() if steps}

    assert set(found) == {"run-agent-pipeline", "generate-pr-content"}
    for steps in found.values():
        assert len(steps) == 1
        assert steps[0]["with"] == TOKEN_WITH
    for job in JOBS.values():
        assert "aud" not in {s.get("id") for s in job["steps"]}
    assert "%%," not in TEXT


@pytest.mark.parametrize(
    "pattern",
    [
        r"gh\s+pr\s+merge",
        r"--auto\b",
        r"gh\s+pr\s+review",
        r"--approve",
        r"--force(?!-with-lease)",
        r"pull_request_target",
        r"GH_PAT",
        r"git\s+push[^\n]*\bmain\b",
    ],
)
def test_forbidden_commands_absent(pattern: str) -> None:
    assert re.search(pattern, TEXT) is None


def test_force_push_only_our_revert_branch() -> None:
    run_text = _create_step()["run"]
    pushes = re.findall(r"git\s+push[^\n]*", run_text)

    assert len(pushes) == 1
    assert "--force-with-lease" in pushes[0]
    assert '"HEAD:refs/heads/${branch}"' in pushes[0]
    assert 'branch="revert-${TARGET_SHA}"' in run_text


def test_revert_pr_pat_only_in_create_rollback_pr() -> None:
    holders = {job_id for job_id, job in JOBS.items() if "REVERT_PR_PAT" in yaml.safe_dump(job)}
    job = JOBS["create-rollback-pr"]
    holder_steps = {s["id"] for s in job["steps"] if "REVERT_PR_PAT" in yaml.safe_dump(s)}
    checkout = next(s for s in job["steps"] if s["id"] == "checkout-deployment")
    create = _create_step()

    assert holders == {"create-rollback-pr"}
    assert holder_steps == {"checkout-deployment", "create"}
    assert checkout["with"]["token"] == PAT
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["fetch-depth"] == 0
    assert create["env"]["GH_TOKEN"] == PAT
    assert job["permissions"] == {"contents": "read"}
    assert job["environment"] == "sentinel-dev"
    assert not any(_is_login(s) for s in job["steps"])
    assert re.search(r"git\s+config[^\n]*(extraheader|credential)", TEXT) is None
    assert "@github.com" not in TEXT
    assert create["run"].index("::add-mask::") < create["run"].index("GIT_CONFIG_VALUE_0")


def test_pr_reported_before_record() -> None:
    job = JOBS["create-rollback-pr"]
    outcome = next(s for s in _steps("summary") if s.get("id") == "outcome")

    assert job["steps"][-1]["id"] == "create"
    assert job["outputs"] == {
        "pr-url": "${{ steps.create.outputs.pr_url }}",
        "pr-number": "${{ steps.create.outputs.pr_number }}",
    }
    assert outcome["env"]["RECORD_RESULT"] == "${{ needs.record-rollback-pr.result }}"
    assert outcome["env"]["PR_URL"] == "${{ needs.create-rollback-pr.outputs.pr-url }}"
    assert "record-rollback-pr" not in _needs(JOBS["notify-rollback"])


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
    record = _find("record-rollback-pr", "psql-exec")

    assert any(
        s.get("with", {}).get("repository") == "Keshav0375/Sentinel-deployment" for s in steps
    )
    for fragment in ("git revert --no-edit -m 1", "gh pr create", "--base main", "--body-file"):
        assert fragment in run_text
    assert record["with"]["sql"].startswith("UPDATE incidents SET pr_number")
    assert record["with"]["pg-user"] == "${{ vars.PG_USER_GHA }}"
    assert not any(_action(s) == "psql-exec" for s in steps)


def test_fetch_pr_details_uses_the_deployment_repo() -> None:
    run_text = "\n".join(s.get("run", "") for s in _steps("fetch-pr-details"))

    assert "repos/${DEPLOYMENT_REPO}/pulls/" in run_text
    assert WF["env"]["DEPLOYMENT_REPO"] == "Keshav0375/Sentinel-deployment"
    assert WF["env"]["DD_SITE"] == "us5.datadoghq.com"


# --- behaviour: the poll loop and the create step under bash with stubbed tools ---

CURL_POLL = r"""
out=""; data=0; prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  [ "$a" = "--data-binary" ] && data=1
  prev="$a"
done
if [ "$data" = 1 ]; then
  printf '{"incident_id":"%s","status":"accepted"}' "$STUB_ID" > "$out"
  printf 202
  exit 0
fi
n=$(( $(grep -c '^curl ' "$STUB_LOG") - 1 ))
line=$(sed -n "${n}p" "$STUB_SEQ")
[ -n "$line" ] || line=$(tail -n 1 "$STUB_SEQ")
code=${line%% *}
status=${line#* }
[ "$code" != 000 ] || exit 7
printf '{"status":"%s"}' "$status" > "$out"
printf '%s' "$code"
"""
INCIDENT_ID = str(uuid.UUID(int=7))


def _poll(
    tmp_path: Path, *seq: str, timeout: str = "600", grace: str = "60"
) -> tuple[int, str, dict[str, str], list[str]]:
    _need("jq")
    (tmp_path / "payload.json").write_text(json.dumps({"title": "t"}))
    (tmp_path / "seq").write_text("".join(f"{line}\n" for line in seq))
    env = {
        "BACKEND_URL": "http://10.0.0.7",
        "BEARER": "tok",
        "POLL_INTERVAL_SECONDS": "0",
        "POLL_TIMEOUT_SECONDS": timeout,
        "POLL_NOT_FOUND_GRACE_SECONDS": grace,
        "STUB_ID": INCIDENT_ID,
        "STUB_SEQ": str(tmp_path / "seq"),
    }
    return _run_script("run-agent-pipeline", "run", env, {"curl": CURL_POLL}, tmp_path)


def _gets(log: list[str]) -> int:
    return sum(1 for line in log if line.startswith("curl ")) - 1


@pytest.mark.parametrize("code", ["401", "403", "400", "422"])
def test_poll_fails_fast_on_client_error(tmp_path: Path, code: str) -> None:
    rc, output, outputs, log = _poll(tmp_path, code)

    assert rc != 0
    assert f"HTTP {code}" in output
    assert _gets(log) == 1
    assert "resolution_type" not in outputs


def test_poll_retries_5xx_and_transport_errors(tmp_path: Path) -> None:
    rc, output, outputs, log = _poll(tmp_path, "503", "000", "200 escalated")

    assert rc == 0, output
    assert outputs["resolution_type"] == "escalated"
    assert _gets(log) == 3


def test_poll_tolerates_a_short_404(tmp_path: Path) -> None:
    rc, output, outputs, _ = _poll(tmp_path, "404", "200 escalated")

    assert rc == 0, output
    assert outputs["resolution_type"] == "escalated"


def test_poll_404_past_grace_is_a_fault(tmp_path: Path) -> None:
    rc, output, outputs, _ = _poll(tmp_path, "404", grace="0")

    assert rc != 0
    assert "not found" in output
    assert "resolution_type" not in outputs


def test_poll_deadline_while_running_is_a_timeout(tmp_path: Path) -> None:
    rc, output, outputs, _ = _poll(tmp_path, "200 in_progress", timeout="0")

    assert rc == 0, output
    assert outputs["evidence_summary"] == "pipeline timeout"


def test_poll_deadline_on_5xx_is_a_fault(tmp_path: Path) -> None:
    rc, _, outputs, _ = _poll(tmp_path, "502", timeout="0")

    assert rc != 0
    assert "evidence_summary" not in outputs


GH_CREATE = (
    'case "$1 $2" in "pr list") printf \'%s\' "${STUB_OPEN_URL:-}";; '
    '"pr create") echo https://github.com/Keshav0375/Sentinel-deployment/pull/42;; *) exit 9;; esac'
)
GIT_CREATE = (
    "printf '%s\\n' \"env ${GIT_CONFIG_COUNT:-}|${GIT_CONFIG_KEY_0:-}|${GIT_CONFIG_VALUE_0:-}\" "
    '>> "$STUB_LOG"\n'
    'case "$1" in rev-parse) echo ' + "abcdef1" + "0" * 33 + ";; rev-list) echo 'x y';; "
    "*) exit 0;; esac"
)


def _create(tmp_path: Path, open_url: str = "") -> tuple[int, str, dict[str, str], list[str]]:
    _need("jq")
    env = {
        "GH_TOKEN": "pat-123",
        "TARGET_SHA": "abcdef1",
        "PR_CONTENT": json.dumps({"title": "Revert x", "description": "d"}),
        "INCIDENT_ID": INCIDENT_ID,
        "DEPLOYMENT_REPO": "Keshav0375/Sentinel-deployment",
        "RUN_URL": "https://x",
        "STUB_OPEN_URL": open_url,
    }
    return _run_script(
        "create-rollback-pr",
        "create",
        env,
        {"gh": GH_CREATE, "git": GIT_CREATE},
        tmp_path,
        tmp_path,
    )


def test_create_reuses_open_pr(tmp_path: Path) -> None:
    url = "https://github.com/Keshav0375/Sentinel-deployment/pull/7"
    rc, output, outputs, log = _create(tmp_path, open_url=url)

    assert rc == 0, output
    assert outputs == {"pr_number": "7", "pr_url": url}
    assert not any(line.startswith("git push") for line in log)
    assert not any(line.startswith("gh pr create") for line in log)


def test_create_force_pushes_revert_branch_with_env_auth(tmp_path: Path) -> None:
    rc, output, outputs, log = _create(tmp_path)

    assert rc == 0, output
    assert outputs["pr_number"] == "42"
    push = log.index("git push --force-with-lease origin HEAD:refs/heads/revert-abcdef1")
    basic = base64.b64encode(b"x-access-token:pat-123").decode()
    # The stub logs its call, then the environment it was given.
    assert log[push + 1] == (
        f"env 1|http.https://github.com/.extraheader|AUTHORIZATION: basic {basic}"
    )
    assert not any("pat-123" in line for line in log if line.startswith("git "))
    assert "::add-mask::" in output
    assert sum(1 for line in log if line.startswith("gh pr create")) == 1


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
