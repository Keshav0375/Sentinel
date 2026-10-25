"""The Azure CI identity preflight of the deployment, incident and scale workflows.

Until the infra CI-identity ticket creates the variables (decisions.md 2026-10-24, phase 8,
"Names the infra CI-identity ticket must create"), these workflows must skip cleanly instead of
failing at azure/login. Each has a `preflight` job, with no secrets and no login, that reports
`configured`; every other job is gated on it. The static tests read the workflows as YAML; the
behavioural tests run the preflight script under the local bash. Nothing here calls GitHub or Azure.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

GATE = "needs.preflight.outputs.configured == 'true'"
NOTICE = (
    "Azure CI identity not configured — {workflow} skipped. See sentinel-brain decisions 2026-10-24"
    " phase 8 'Names the infra CI-identity ticket must create'."
)
# The identity every workflow logs in with, whatever else it needs.
CORE_VARS = {"AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"}
# Optional: an empty SENTINEL_KEEP_WARM means "scale down" (backend-down defaults it to false).
OPTIONAL_VARS = {"SENTINEL_KEEP_WARM"}

# Workflow file -> the jobs that run under always() (teardown and reporting).
WORKFLOWS: dict[str, set[str]] = {
    "ci_backend_deployment.yml": {"promote-or-rollback"},
    "ci_incident_response.yml": {"teardown-backend", "summary"},
    "ci_backend_scale.yml": set(),
}
WORKFLOW_IDS = list(WORKFLOWS)

LOOP_RE = re.compile(r"for name in (.*?); do", re.DOTALL)


def _doc(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _jobs(name: str) -> dict[str, dict[str, Any]]:
    return _doc(name)["jobs"]


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def _if(job: dict[str, Any]) -> str:
    """The job condition with `${{ }}` and folding whitespace removed."""
    condition = " ".join(str(job.get("if", "")).split())
    if condition.startswith("${{") and condition.endswith("}}"):
        condition = condition[3:-2].strip()
    return condition


def _check_step(name: str) -> dict[str, Any]:
    steps = _jobs(name)["preflight"]["steps"]
    assert [s.get("id") for s in steps] == ["check"]
    return steps[0]


def _checked_names(name: str) -> list[str]:
    match = LOOP_RE.search(_check_step(name)["run"])
    assert match, "the preflight loops over the required variable names"
    return match.group(1).replace("\\\n", " ").split()


def _vars_used(name: str) -> set[str]:
    text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
    return set(re.findall(r"vars\.([A-Za-z0-9_]+)", text))


def _run_preflight(
    name: str, env: dict[str, str], tmp_path: Path
) -> tuple[int, str, dict[str, str], str]:
    output = tmp_path / "github_output"
    summary = tmp_path / "step_summary"
    output.write_text("")
    summary.write_text("")
    proc = subprocess.run(
        ["bash", "-c", _check_step(name)["run"]],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_WORKFLOW": _doc(name)["name"],
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = dict(
        line.partition("=")[::2] for line in output.read_text().splitlines() if "=" in line
    )
    return proc.returncode, proc.stdout + proc.stderr, outputs, summary.read_text()


def _all_set(name: str) -> dict[str, str]:
    return {var: f"value-of-{var.lower()}" for var in _checked_names(name)}


# --- static ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", WORKFLOW_IDS)
def test_preflight_job_is_first_and_cloud_free(name: str) -> None:
    jobs = _jobs(name)
    assert next(iter(jobs)) == "preflight"
    job = jobs["preflight"]
    others = [j for job_id, j in jobs.items() if job_id != "preflight"]

    assert job["name"] == "Check Azure CI identity"
    assert job["permissions"] == {}
    assert "needs" not in job
    assert "if" not in job
    assert isinstance(job["timeout-minutes"], int)
    assert {job["runs-on"]} == {j["runs-on"] for j in others}
    assert job["outputs"] == {"configured": "${{ steps.check.outputs.configured }}"}
    dumped = yaml.safe_dump(job)
    assert "secrets." not in dumped
    assert "azure/login" not in dumped
    assert all("uses" not in step for step in job["steps"])


@pytest.mark.parametrize("name", WORKFLOW_IDS)
def test_preflight_run_has_no_expressions(name: str) -> None:
    for step in _jobs(name)["preflight"]["steps"]:
        if "run" in step:
            assert "${{" not in step["run"]
            assert step["run"].startswith("set -euo pipefail")


@pytest.mark.parametrize("name", WORKFLOW_IDS)
def test_preflight_checks_every_required_var(name: str) -> None:
    step = _check_step(name)
    checked = _checked_names(name)

    assert len(checked) == len(set(checked))
    assert set(checked) >= CORE_VARS
    assert set(checked) == _vars_used(name) - OPTIONAL_VARS
    # Each variable reaches the script through env:, under its own name.
    assert step["env"] == {var: f"${{{{ vars.{var} }}}}" for var in checked}


@pytest.mark.parametrize("name", WORKFLOW_IDS)
def test_every_other_job_is_gated_on_preflight(name: str) -> None:
    for job_id, job in _jobs(name).items():
        if job_id == "preflight":
            continue
        assert "preflight" in _needs(job), job_id
        assert GATE in _if(job), job_id


@pytest.mark.parametrize("name", WORKFLOW_IDS)
def test_always_jobs_keep_always_and_the_gate(name: str) -> None:
    jobs = _jobs(name)
    always = {job_id for job_id, job in jobs.items() if "always()" in _if(job)}

    assert always == WORKFLOWS[name]
    for job_id in always:
        assert _if(jobs[job_id]) == f"always() && {GATE}", job_id


@pytest.mark.parametrize("name", WORKFLOW_IDS)
def test_gated_conditions_keep_their_status_check(name: str) -> None:
    # A job that already had a status check keeps it in front of the gate; the rest rely on the
    # implicit success().
    for job_id, job in _jobs(name).items():
        condition = _if(job)
        if job_id == "preflight" or condition == GATE:
            continue
        assert condition.startswith(("always() && ", "!cancelled() && ")), job_id
        assert f"&& {GATE}" in condition, job_id


# --- behaviour ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", WORKFLOW_IDS)
def test_configured_when_every_var_is_set(name: str, tmp_path: Path) -> None:
    rc, out, outputs, summary = _run_preflight(name, _all_set(name), tmp_path)

    assert rc == 0, out
    assert outputs == {"configured": "true"}
    assert "::notice::" not in out
    assert summary == ""


@pytest.mark.parametrize("name", WORKFLOW_IDS)
def test_unconfigured_run_skips_with_notice_and_summary(name: str, tmp_path: Path) -> None:
    rc, out, outputs, summary = _run_preflight(name, {}, tmp_path)
    notice = NOTICE.format(workflow=_doc(name)["name"])

    assert rc == 0, out
    assert outputs == {"configured": "false"}
    assert f"::notice::{notice}" in out
    assert notice in summary
    for var in _checked_names(name):
        assert var in summary, var


@pytest.mark.parametrize("name", WORKFLOW_IDS)
def test_any_single_missing_or_empty_var_is_unconfigured(name: str, tmp_path: Path) -> None:
    for var in _checked_names(name):
        for env in (
            {k: v for k, v in _all_set(name).items() if k != var},
            {**_all_set(name), var: ""},
        ):
            rc, out, outputs, summary = _run_preflight(name, env, tmp_path)
            assert rc == 0, out
            assert outputs == {"configured": "false"}, var
            assert summary.rstrip().endswith(f"Missing variables: {var}"), var
