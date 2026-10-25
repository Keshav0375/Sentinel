"""Static contract of the infra workflows after the monorepo move
(infra phase 7, decisions 2026-10-25).

Sentinel-infra was imported as infra/ with its history; its four workflows now live at the root as
.github/workflows/infra_*.yml, run in infra/, and keep every Azure job skipped until the repo
variable INFRA_OIDC_READY is true. The static tests read the workflows as YAML; the behavioural
test runs the readiness script under the local bash. Nothing here calls GitHub or Azure.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WF = REPO_ROOT / ".github" / "workflows"
INFRA = {"infra_deploy.yml", "infra_validate_plan.yml", "infra_pause.yml", "infra_runners.yml"}
INFRA_IDS = sorted(INFRA)

GATE = "needs.oidc-ready.outputs.ready == 'true'"
P = ["infra/**", ".github/workflows/infra_*.yml"]

# The run steps that execute before actions/checkout, so before infra/ exists.
PRE_CHECKOUT: dict[str, dict[str, list[str]]] = {
    "infra_deploy.yml": {"run": ["Mask the administrator UPN", "Check guards"]},
    "infra_validate_plan.yml": {"run-plan": ["Mask the administrator UPN"]},
    "infra_pause.yml": {},
    "infra_runners.yml": {},
}

# The vars each workflow read before the move, plus the readiness gate's own.
EXPECTED_VARS: dict[str, set[str]] = {
    "infra_deploy.yml": {
        "AZURE_CLIENT_ID_DEPLOY",
        "AZURE_CLIENT_ID_PLAN",
        "AZURE_LOCATION",
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_TENANT_ID",
        "GHA_APP_OBJECT_ID",
        "KV_ADMIN_OBJECT_ID",
        "PG_ADMIN_OBJECT_ID",
        "PG_ADMIN_PRINCIPAL_NAME",
        "INFRA_OIDC_READY",
    },
    "infra_validate_plan.yml": {
        "AZURE_CLIENT_ID_PLAN",
        "AZURE_LOCATION",
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_TENANT_ID",
        "GHA_APP_OBJECT_ID",
        "KV_ADMIN_OBJECT_ID",
        "PG_ADMIN_OBJECT_ID",
        "PG_ADMIN_PRINCIPAL_NAME",
        "INFRA_OIDC_READY",
    },
    "infra_pause.yml": {
        "AZURE_CLIENT_ID_OPS",
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_TENANT_ID",
        "INFRA_OIDC_READY",
    },
    "infra_runners.yml": {
        "AZURE_CLIENT_ID_DEPLOY",
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_TENANT_ID",
        "INFRA_OIDC_READY",
    },
}


def _text(name: str) -> str:
    return (WF / name).read_text(encoding="utf-8")


def _doc(name: str) -> dict[str, Any]:
    return yaml.safe_load(_text(name))


def _jobs(name: str) -> dict[str, dict[str, Any]]:
    return _doc(name)["jobs"]


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    """The trigger block. PyYAML reads a bare `on:` key as boolean True."""
    return doc["on"] if "on" in doc else doc[True]


def _if(job: dict[str, Any]) -> str:
    """The job condition with `${{ }}` and folding whitespace removed."""
    condition = " ".join(str(job.get("if", "")).split())
    if condition.startswith("${{") and condition.endswith("}}"):
        condition = condition[3:-2].strip()
    return condition


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def test_infra_workflow_set() -> None:
    assert {p.name for p in WF.glob("infra_*.yml")} == INFRA
    assert not (REPO_ROOT / "infra" / ".github").exists()


@pytest.mark.parametrize("wf", INFRA_IDS)
def test_jobs_run_in_infra(wf: str) -> None:
    jobs = _jobs(wf)
    assert "defaults" not in jobs["oidc-ready"]
    for name, job in jobs.items():
        if name == "oidc-ready":
            continue
        assert job["defaults"]["run"]["working-directory"] == "infra", f"{wf}:{name}"


@pytest.mark.parametrize("wf", INFRA_IDS)
def test_pre_checkout_steps_use_workspace(wf: str) -> None:
    found: dict[str, list[str]] = {}
    for name, job in _jobs(wf).items():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                break
            if "run" in step and name != "oidc-ready":
                assert step.get("working-directory") == ".", f"{wf}:{name}:{step.get('name')}"
                found.setdefault(name, []).append(step["name"])
    assert found == PRE_CHECKOUT[wf]


@pytest.mark.parametrize("wf", INFRA_IDS)
def test_no_expression_inside_run_blocks(wf: str) -> None:
    for name, job in _jobs(wf).items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), f"{wf}:{name}:{step.get('name')}"


@pytest.mark.parametrize("wf", INFRA_IDS)
def test_azure_jobs_gated(wf: str) -> None:
    gated = 0
    for name, job in _jobs(wf).items():
        if any(str(s.get("uses", "")).startswith("azure/login@") for s in job["steps"]):
            assert "oidc-ready" in _needs(job), f"{wf}:{name}"
            assert GATE in _if(job), f"{wf}:{name}"
            gated += 1
    assert gated >= 1


def test_validate_job_ungated() -> None:
    job = _jobs("infra_validate_plan.yml")["run-validate"]
    assert "needs" not in job
    assert "if" not in job
    assert "environment" not in job
    assert not any(str(s.get("uses", "")).startswith("azure/login@") for s in job["steps"])
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    for command in (
        "terraform fmt -check -recursive",
        "terraform validate",
        "-chdir=modules/naming test",
    ):
        assert command in runs


@pytest.mark.parametrize("wf", INFRA_IDS)
def test_gate_job_secret_free(wf: str) -> None:
    job = _jobs(wf)["oidc-ready"]
    assert job["permissions"] == {}
    assert "environment" not in job
    assert [s.get("id") for s in job["steps"]] == ["check"]
    assert job["steps"][0]["env"] == {"INFRA_OIDC_READY": "${{ vars.INFRA_OIDC_READY }}"}
    assert job["outputs"] == {"ready": "${{ steps.check.outputs.ready }}"}
    assert "secrets." not in yaml.safe_dump(job)


@pytest.mark.parametrize("value", [None, "", "false", "TRUE", "true"])
def test_gate_behaviour(value: str | None, tmp_path: Path) -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not on PATH")
    script = _jobs("infra_pause.yml")["oidc-ready"]["steps"][0]["run"]
    out, summary = tmp_path / "out", tmp_path / "sum"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_WORKFLOW": "t",
    }
    if value is not None:
        env["INFRA_OIDC_READY"] = value
    proc = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    if value == "true":
        assert out.read_text(encoding="utf-8") == "ready=true\n"
        assert not summary.exists() or summary.read_text(encoding="utf-8") == ""
    else:
        assert "ready=false" in out.read_text(encoding="utf-8")
        assert "::notice::" in proc.stdout
        assert "INFRA_OIDC_READY" in summary.read_text(encoding="utf-8")


def test_triggers() -> None:
    validate = _on(_doc("infra_validate_plan.yml"))
    assert validate["push"]["branches"] == ["**"]
    assert validate["push"]["paths"] == P
    assert validate["pull_request"]["branches"] == ["main"]
    assert validate["pull_request"]["paths"] == P
    assert "workflow_dispatch" in validate

    runners = _on(_doc("infra_runners.yml"))
    assert runners["push"] == {"branches": ["main"], "paths": ["infra/ci-images/**"]}

    for wf in ("infra_deploy.yml", "infra_pause.yml"):
        assert set(_on(_doc(wf))) == {"workflow_dispatch"}, wf


def test_concurrency_groups() -> None:
    assert {wf: _doc(wf)["concurrency"]["group"] for wf in INFRA} == {
        "infra_deploy.yml": "infra-apply",
        "infra_validate_plan.yml": "infra-dry-${{ github.ref }}",
        "infra_pause.yml": "infra-pause",
        "infra_runners.yml": "infra-runners",
    }


def test_environments_preserved() -> None:
    deploy = _jobs("infra_deploy.yml")
    assert deploy["run"]["environment"] == (
        "${{ inputs.action == 'destroy' && 'destroy' || 'production' }}"
    )
    assert deploy["verify"]["environment"] == "plan"
    validate = _jobs("infra_validate_plan.yml")
    assert validate["run-preflight"]["environment"] == "plan"
    assert validate["run-plan"]["environment"] == "plan"
    assert _jobs("infra_pause.yml")["pause-resume"]["environment"] == "ops"
    assert _jobs("infra_runners.yml")["build-and-push"]["environment"] == "production"


@pytest.mark.parametrize("wf", INFRA_IDS)
def test_vars_preserved(wf: str) -> None:
    assert set(re.findall(r"vars\.([A-Za-z0-9_]+)", _text(wf))) == EXPECTED_VARS[wf]


def test_secrets_preserved() -> None:
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", _text("infra_deploy.yml"))) == {
        "DATADOG_API_KEY",
        "DATADOG_APP_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "TEAMS_WEBHOOK_URL",
        "DISPATCH_PAT",
    }
    for wf in ("infra_validate_plan.yml", "infra_pause.yml", "infra_runners.yml"):
        assert "secrets." not in _text(wf), wf


def test_backend_workflows_ignore_infra() -> None:
    assert _on(_doc("ci.yml"))["pull_request"]["paths-ignore"] == ["infra/**"]
    for wf, event in (("ci_validation.yml", "pull_request"), ("ci_backend_deployment.yml", "push")):
        paths = _on(_doc(wf))[event]["paths"]
        after = paths[paths.index(".github/workflows/**") + 1]
        assert after == "!.github/workflows/infra_*.yml", wf
        assert not any(p.startswith("infra/") for p in paths), wf


def test_bootstrap_targets_monorepo() -> None:
    scripts = REPO_ROOT / "infra" / "scripts"
    bootstrap = (scripts / "bootstrap-identities.sh").read_text(encoding="utf-8")
    assert 'INFRA_REPO="${INFRA_REPO:-Sentinel}"' in bootstrap
    assert "federated-credential update" in bootstrap
    secrets = (scripts / "set-gh-secrets.sh").read_text(encoding="utf-8")
    assert 'REPO="Keshav0375/Sentinel"' in secrets


def test_actionlint_clean() -> None:
    if shutil.which("actionlint") is None:
        pytest.skip("actionlint not on PATH")
    files = [str(WF / name) for name in INFRA_IDS]
    proc = subprocess.run(
        ["actionlint", "-no-color", *files],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
