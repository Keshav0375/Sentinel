"""Offline contract checks for .github/workflows/ci_backend_deployment.yml (backend task 8.2).

Sources: architecture backend.md section 9.2 as amended by decisions.md 2026-10-24
(phase 7 and phase 8).
The static tests read the workflow as YAML. The behavioural tests run the verdict, ACR-cleanup and
rollback scripts for real under the local bash (3.2 on macOS, 5.x on the runner) with stubbed az and
kubectl. Nothing here calls Azure, a cluster or the network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci_backend_deployment.yml"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

JOBS = (
    "preflight",
    "run-quality",
    "build-and-push",
    "deploy-to-aks",
    "run-tests",
    "promote-or-rollback",
)

PATHS = [
    "src/**",
    "tests/**",
    "pyproject.toml",
    "poetry.lock",
    "Dockerfile",
    "azure/**",
    ".github/workflows/**",
    "!.github/workflows/infra_*.yml",
    ".github/actions/**",
    "alembic/**",
    "alembic.ini",
    "scripts/**",
]

ALLOWED_VARS = {
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
REQUIRED_VARS = ALLOWED_VARS - {"PG_USER_GHA"}

PIN_RE = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$"
LOCAL_RE = r"^\./\.github/actions/[a-z-]+$"

YAMLLINT_CONFIG = """\
extends: default
rules:
  truthy:
    check-keys: false
  line-length:
    max: 120
  document-start: disable
"""

RESOURCE_LOGIN = {
    "client-id": "${{ vars.AZURE_CLIENT_ID }}",
    "tenant-id": "${{ vars.AZURE_TENANT_ID }}",
    "subscription-id": "${{ vars.AZURE_SUBSCRIPTION_ID }}",
}
IDENTITY_LOGIN = {
    "client-id": "${{ vars.AZURE_GHA_CLIENT_ID }}",
    "tenant-id": "${{ vars.AZURE_IDENTITY_TENANT_ID }}",
    "allow-no-subscriptions": True,
}

BACKEND_UP_WITH = {
    "namespace": "${{ env.NAMESPACE }}",
    "resource-group": "${{ vars.AKS_RESOURCE_GROUP }}",
    "cluster-name": "${{ vars.AKS_CLUSTER_NAME }}",
    "image-tag": "${{ needs.build-and-push.outputs.image-tag }}",
    "acr-login-server": "${{ vars.ACR_LOGIN_SERVER }}",
    "api-tenant-id": "${{ vars.SENTINEL_API_TENANT_ID }}",
    "api-audience": "${{ vars.SENTINEL_API_AUDIENCE }}",
    "key-vault-url": "${{ vars.KEY_VAULT_URI }}",
    "pg-host": "${{ vars.PG_HOST }}",
    "pg-database": "${{ vars.PG_DATABASE }}",
    "pg-user": "${{ vars.PG_USER_BACKEND }}",
}

PREFLIGHT_GATE = "needs.preflight.outputs.configured == 'true'"

NEW_IMAGE = "acr0.azurecr.io/sentinel-backend:sha-abcdef0"


def _text() -> str:
    return WORKFLOW.read_text()


def _doc() -> dict[str, Any]:
    return yaml.safe_load(_text())


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    return doc.get("on", doc.get(True))


def _job(name: str) -> dict[str, Any]:
    return _doc()["jobs"][name]


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job["steps"]


def _step(job: dict[str, Any], step_id: str) -> dict[str, Any]:
    matches = [s for s in _steps(job) if s.get("id") == step_id]
    assert len(matches) == 1, f"step {step_id!r} must exist exactly once"
    return matches[0]


def _script(job_name: str, step_id: str) -> str:
    return _step(_job(job_name), step_id)["run"]


def _need(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} is not installed")


def _stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "{name} $*" >> "$STUB_LOG"\n{body}\n')
    path.chmod(0o755)


def _run(
    script: str,
    env: dict[str, str],
    tmp_path: Path,
    stubs: dict[str, str] | None = None,
) -> tuple[int, str, dict[str, str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (stubs or {}).items():
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


# --- static contract ---------------------------------------------------------------------------


def test_name_and_push_trigger() -> None:
    doc = _doc()
    assert doc["name"] == "[sentinel] backend — deployment"
    on = _on(doc)
    assert set(on) == {"push"}
    assert on["push"]["branches"] == ["main", "release-phase-2"]
    assert set(on["push"]["paths"]) == set(PATHS)
    assert len(on["push"]["paths"]) == len(PATHS)


def test_concurrency_serialises() -> None:
    assert _doc()["concurrency"] == {"group": "sentinel-backend", "cancel-in-progress": False}


def test_jobs_and_needs_chain() -> None:
    jobs = _doc()["jobs"]
    assert list(jobs) == list(JOBS)
    assert "needs" not in jobs["preflight"]
    assert jobs["run-quality"]["needs"] == "preflight"
    assert jobs["build-and-push"]["needs"] == ["preflight", "run-quality"]
    assert jobs["deploy-to-aks"]["needs"] == ["preflight", "build-and-push"]
    assert jobs["run-tests"]["needs"] == ["preflight", "deploy-to-aks"]
    for name in ("run-quality", "build-and-push", "deploy-to-aks", "run-tests"):
        assert jobs[name]["if"] == PREFLIGHT_GATE, name
    assert set(jobs["promote-or-rollback"]["needs"]) == set(JOBS) - {"promote-or-rollback"}


def test_all_jobs_on_arm_runner() -> None:
    for name, job in _doc()["jobs"].items():
        assert job["runs-on"] == "ubuntu-24.04-arm", name
        assert isinstance(job["timeout-minutes"], int), name


def test_login_jobs_use_environment_and_oidc() -> None:
    doc = _doc()
    assert doc["permissions"] == {"contents": "read"}
    no_login = []
    for name, job in doc["jobs"].items():
        logs_in = any(str(s.get("uses", "")).startswith("azure/login@") for s in job["steps"])
        if logs_in:
            assert job["environment"] == "sentinel-dev", name
            assert job["permissions"] == {"id-token": "write", "contents": "read"}, name
        elif name == "preflight":
            # Reads the (environment-level) variables only: no token, no secrets.
            no_login.append(name)
            assert job["environment"] == "sentinel-dev", name
            assert job["permissions"] == {}, name
        else:
            no_login.append(name)
            assert "environment" not in job, name
            assert "permissions" not in job, name
    assert no_login == ["preflight", "run-quality"]


def test_azure_login_inputs() -> None:
    for name, job in _doc()["jobs"].items():
        for step in job["steps"]:
            if not str(step.get("uses", "")).startswith("azure/login@"):
                continue
            if name == "run-tests":
                assert step["with"] == IDENTITY_LOGIN, step["id"]
            else:
                assert step["with"] == RESOURCE_LOGIN, f"{name}/{step['id']}"
    identity = [
        s["id"]
        for s in _steps(_job("run-tests"))
        if str(s.get("uses", "")).startswith("azure/login@")
    ]
    assert identity == ["login-identity"]


def test_actions_pinned_and_checkout_hardened() -> None:
    for name, job in _doc()["jobs"].items():
        seen_checkout = False
        for step in job["steps"]:
            uses = step.get("uses")
            if uses is None:
                continue
            assert re.match(PIN_RE, uses) or re.match(LOCAL_RE, uses), f"{name}: {uses}"
            if uses.startswith("actions/checkout@"):
                seen_checkout = True
                assert step["with"]["persist-credentials"] is False, name
            if re.match(LOCAL_RE, uses):
                assert seen_checkout, f"{name}: local action {uses} before checkout"


def test_no_interpolation_in_run() -> None:
    for name, job in _doc()["jobs"].items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), f"{name}/{step.get('id')}"
    text = _text()
    for forbidden in ("secrets.", "github.event", "head_ref"):
        assert forbidden not in text, forbidden


def test_var_names_from_decision() -> None:
    used = set(re.findall(r"vars\.([A-Za-z0-9_]+)", _text()))
    assert used <= ALLOWED_VARS, used - ALLOWED_VARS
    assert used >= REQUIRED_VARS, REQUIRED_VARS - used


def test_sha_tag_only_never_latest() -> None:
    text = _text()
    assert "latest" not in text.lower()
    job = _job("build-and-push")
    assert "sha-${GITHUB_SHA::7}" in _step(job, "tag")["run"]
    assert (
        'docker push "$ACR_LOGIN_SERVER/sentinel-backend:$IMAGE_TAG"' in _step(job, "push")["run"]
    )
    pushes = [
        (name, s["id"])
        for name, j in _doc()["jobs"].items()
        for s in j["steps"]
        if "docker push" in s.get("run", "") or "docker tag" in s.get("run", "")
    ]
    assert pushes == [("build-and-push", "push")]
    assert job["outputs"]["image-tag"] == "${{ steps.tag.outputs.image-tag }}"


def test_deploy_uses_backend_up_with_all_inputs() -> None:
    job = _job("deploy-to-aks")
    up = _step(job, "up")
    assert up["uses"] == "./.github/actions/backend-up"
    action = yaml.safe_load((ACTIONS_DIR / "backend-up" / "action.yml").read_text())
    assert set(up["with"]) == set(action["inputs"])
    assert up["with"] == BACKEND_UP_WITH
    assert job["outputs"]["backend-url"] == "${{ steps.up.outputs.backend-url }}"


def test_run_tests_smoke_contract() -> None:
    job = _job("run-tests")
    assert job["services"]["postgres"]["image"] == "pgvector/pgvector:pg16"
    ids = [s["id"] for s in _steps(job)]
    order = ["unit", "integration", "login-identity", "token", "smoke"]
    assert [ids.index(i) for i in order] == sorted(ids.index(i) for i in order)
    token = _step(job, "token")
    assert token["uses"] == "./.github/actions/get-backend-token"
    assert token["with"] == {
        "audience": "${{ vars.SENTINEL_API_AUDIENCE }}",
        "tenant": "${{ vars.AZURE_IDENTITY_TENANT_ID }}",
    }
    smoke = _step(job, "smoke")
    assert smoke["env"]["BEARER"] == "${{ steps.token.outputs.bearer }}"
    for needle in (
        "/webhooks/incident",
        "/incidents/$id",
        "rollback_proposed",
        "escalated",
        "1200",
        "-H @",
    ):
        assert needle in smoke["run"], needle


def test_promote_if_always() -> None:
    job = _job("promote-or-rollback")
    assert job["if"] == f"always() && {PREFLIGHT_GATE}"
    assert "mode == 'promote'" in _step(job, "stable")["if"]
    assert "mode == 'promote'" in _step(job, "cleanup")["if"]
    stable = _step(job, "stable")["run"]
    assert "imagetools create" in stable
    assert ":stable" in stable
    assert "acr import" not in _text()


def test_rollback_undo_path() -> None:
    doc = _doc()
    assert doc["env"]["NAMESPACE"] == "sentinel-dev"
    job = _job("promote-or-rollback")
    undo = _step(job, "undo")
    assert "always()" in undo["if"]
    assert "mode == 'rollback'" in undo["if"]
    assert 'kubectl -n "$NAMESPACE" rollout undo deployment/sentinel-backend' in undo["run"]
    kv = _step(job, "kv")
    assert kv["uses"] == "./.github/actions/get-kv-secrets"
    assert kv["with"] == {"vault-uri": "${{ vars.KEY_VAULT_URI }}", "names": "teams-webhook-url"}
    notify = _step(job, "notify")
    assert notify["uses"] == "./.github/actions/notify-teams"
    assert notify["with"]["webhook-url"] == "${{ steps.kv.outputs.teams-webhook-url }}"


def test_keep_warm_teardown_after_relogin() -> None:
    steps = _steps(_job("promote-or-rollback"))
    down, relogin = steps[-1], steps[-2]
    assert down["id"] == "down"
    assert down["uses"] == "./.github/actions/backend-down"
    assert down["if"] == "always()"
    assert down["with"]["keep-warm"] == "${{ vars.SENTINEL_KEEP_WARM }}"
    assert relogin["id"] == "relogin"
    assert relogin["uses"].startswith("azure/login@")
    assert relogin["if"] == "always()"


# --- behaviour ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run_tests", "deploy", "mode"),
    [
        ("success", "success", "promote"),
        ("failure", "success", "rollback"),
        ("skipped", "failure", "rollback"),
        ("cancelled", "cancelled", "rollback"),
        ("skipped", "skipped", "none"),
        ("", "", "none"),
    ],
)
def test_verdict_modes(tmp_path: Path, run_tests: str, deploy: str, mode: str) -> None:
    rc, out, outputs, _ = _run(
        _script("promote-or-rollback", "verdict"),
        {"RUN_TESTS": run_tests, "DEPLOY": deploy},
        tmp_path,
    )
    assert rc == 0, out
    assert outputs["mode"] == mode


def _tags(stable_digest: str) -> str:
    entries = [
        ("stable", stable_digest),
        ("sha-1111111", "d1"),
        ("sha-2222222", "d2"),
        ("sha-3333333", "d3"),
        ("sha-4444444", "d4"),
        ("sha-5555555", "d5"),
        ("sha-6666666", "d6"),
        ("sha-7777777", "d2"),
        ("keep-x", "d9"),
    ]
    return json.dumps([{"name": n, "digest": f"sha256:{d}"} for n, d in entries])


def _cleanup(tmp_path: Path, stable_digest: str, kubectl_image: str) -> tuple[int, str, list[str]]:
    rc, out, _, log = _run(
        _script("promote-or-rollback", "cleanup"),
        {"ACR_NAME": "acr0", "NAMESPACE": "sentinel-dev", "TAGS_JSON": _tags(stable_digest)},
        tmp_path,
        stubs={
            "az": 'if [ "$3" = "show-tags" ]; then printf \'%s\' "$TAGS_JSON"; fi',
            "kubectl": f"echo {kubectl_image}",
        },
    )
    return (
        rc,
        out,
        [
            line
            for line in log
            if line.startswith("az acr repository ") and " show-tags" not in line
        ],
    )


def test_cleanup_keeps_newest_stable_and_running(tmp_path: Path) -> None:
    _need("jq")
    rc, out, calls = _cleanup(tmp_path, "d1", "acr0.azurecr.io/sentinel-backend:sha-5555555")
    assert rc == 0, out
    assert sorted(calls) == [
        "az acr repository delete --name acr0 --image sentinel-backend:sha-4444444 --yes",
        "az acr repository delete --name acr0 --image sentinel-backend:sha-6666666 --yes",
        "az acr repository untag --name acr0 --image sentinel-backend:sha-7777777",
    ]
    assert not any("stable" in c or "keep-x" in c for c in calls)


def test_cleanup_keeps_tag_behind_stable(tmp_path: Path) -> None:
    _need("jq")
    rc, out, calls = _cleanup(tmp_path, "d6", "acr0.azurecr.io/sentinel-backend:sha-5555555")
    assert rc == 0, out
    assert not any("sha-6666666" in c for c in calls)
    assert any("delete" in c and "sha-4444444" in c for c in calls)
    assert not any("stable" in c or "keep-x" in c for c in calls)


def test_cleanup_refuses_without_running_tag(tmp_path: Path) -> None:
    _need("jq")
    rc, _, calls = _cleanup(tmp_path, "d1", "acr0.azurecr.io/sentinel-backend:")
    assert rc != 0
    assert calls == []


def _undo(
    tmp_path: Path, live_image: str, history: str
) -> tuple[int, str, dict[str, str], list[str]]:
    kubectl = (
        'case "$*" in\n'
        f'  *"get deployment"*) echo "{live_image}";;\n'
        f'  *"rollout history"*) printf \'%b\' "{history}";;\n'
        "esac"
    )
    return _run(
        _script("promote-or-rollback", "undo"),
        {
            "IMAGE_TAG": "sha-abcdef0",
            "ACR_LOGIN_SERVER": "acr0.azurecr.io",
            "NAMESPACE": "sentinel-dev",
        },
        tmp_path,
        stubs={"kubectl": kubectl},
    )


TWO_REVISIONS = (
    "deployment.apps/sentinel-backend\\nREVISION  CHANGE-CAUSE\\n1  <none>\\n2  <none>\\n"
)
ONE_REVISION = "deployment.apps/sentinel-backend\\nREVISION  CHANGE-CAUSE\\n1  <none>\\n"


def test_undo_when_new_image_live(tmp_path: Path) -> None:
    rc, out, outputs, log = _undo(tmp_path, NEW_IMAGE, TWO_REVISIONS)
    assert rc == 0, out
    assert outputs["undone"] == "true"
    undo = log.index("kubectl -n sentinel-dev rollout undo deployment/sentinel-backend")
    assert any(
        line.startswith("kubectl -n sentinel-dev rollout status") for line in log[undo + 1 :]
    )


def test_undo_skipped_when_new_image_not_live(tmp_path: Path) -> None:
    rc, out, outputs, log = _undo(
        tmp_path, "acr0.azurecr.io/sentinel-backend:sha-1234567", TWO_REVISIONS
    )
    assert rc == 0, out
    assert outputs["undone"] == "false"
    assert not any("rollout undo" in line for line in log)


def test_undo_scales_to_zero_without_history(tmp_path: Path) -> None:
    rc, out, outputs, log = _undo(tmp_path, NEW_IMAGE, ONE_REVISION)
    assert rc == 0, out
    assert outputs["undone"] == "scaled-to-zero"
    assert "kubectl -n sentinel-dev scale deployment/sentinel-backend --replicas=0" in log
    assert not any("rollout undo" in line for line in log)


# --- linters -----------------------------------------------------------------------------------


def test_actionlint_workflow(tmp_path: Path) -> None:
    _need("actionlint")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    shutil.copy(WORKFLOW, tmp_path / ".github" / "workflows" / WORKFLOW.name)
    shutil.copytree(ACTIONS_DIR, tmp_path / ".github" / "actions")
    (tmp_path / ".git").mkdir()
    proc = subprocess.run(
        ["actionlint", "-no-color"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_shellcheck_run_blocks(tmp_path: Path) -> None:
    _need("shellcheck")
    failures = []
    for name, job in _doc()["jobs"].items():
        for step in job["steps"]:
            if "run" not in step:
                continue
            script = tmp_path / f"{name}-{step['id']}.sh"
            script.write_text("#!/usr/bin/env bash\n" + step["run"])
            proc = subprocess.run(
                ["shellcheck", "--shell=bash", "--severity=style", str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                failures.append(proc.stdout + proc.stderr)
    assert not failures, "\n".join(failures)


def test_yamllint_workflow() -> None:
    _need("yamllint")
    proc = subprocess.run(
        ["yamllint", "-s", "-d", YAMLLINT_CONFIG, str(WORKFLOW)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
