"""Offline contract checks for ci_validation.yml and guard-main-source.yml.

Backend 8.1; decisions 2026-10-24.
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
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI_VALIDATION = WORKFLOWS / "ci_validation.yml"
GUARD = WORKFLOWS / "guard-main-source.yml"
# Orchestrator ruling (brain 8b72023): alembic/**, alembic.ini and scripts/** also
# change the image or the pipeline.
PATHS = (
    "src/**",
    "tests/**",
    "pyproject.toml",
    "poetry.lock",
    "Dockerfile",
    "azure/**",
    "alembic/**",
    "alembic.ini",
    "scripts/**",
    ".github/workflows/**",
    ".github/actions/**",
)
JOBS = ("run-quality", "build-and-test")


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    return doc["on"] if "on" in doc else doc[True]


def _job(name: str) -> dict[str, Any]:
    return _load(CI_VALIDATION)["jobs"][name]


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job["steps"]


def _step(job_name: str, step_id: str) -> dict[str, Any]:
    for step in _steps(_job(job_name)):
        if step.get("id") == step_id:
            return step
    pytest.fail(f"no step with id {step_id!r} in job {job_name!r}")


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(s.get("run", "") for s in _steps(job))


def _need(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not on PATH")


def test_workflow_name() -> None:
    assert _load(CI_VALIDATION)["name"] == "[sentinel] PR — validation"


def test_trigger_is_pull_request_into_main_and_release() -> None:
    trigger = _on(_load(CI_VALIDATION))
    assert set(trigger) == {"pull_request"}
    assert trigger["pull_request"]["branches"] == ["main", "release-phase-2"]
    assert tuple(trigger["pull_request"]["paths"]) == PATHS
    assert "pull_request_target" not in CI_VALIDATION.read_text()


def test_permissions_read_only() -> None:
    doc = _load(CI_VALIDATION)
    assert doc["permissions"] == {"contents": "read"}
    for name, job in doc["jobs"].items():
        assert "permissions" not in job, f"job {name} overrides permissions"


def test_no_cloud_identity_environment_or_secrets() -> None:
    doc = _load(CI_VALIDATION)
    for name, job in doc["jobs"].items():
        assert "environment" not in job, f"job {name} targets an environment"
        for step in _steps(job):
            assert not str(step.get("uses", "")).startswith("azure/")
    text = CI_VALIDATION.read_text()
    for banned in (
        "id-token",
        "azure/login",
        "secrets.",
        "docker push",
        "docker login",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        assert banned not in text, f"{banned!r} must not appear in the PR gate"


def test_jobs_names_needs_and_runner() -> None:
    jobs = _load(CI_VALIDATION)["jobs"]
    assert set(jobs) == set(JOBS)
    assert jobs["run-quality"]["name"] == "Run Quality Checks"
    assert jobs["build-and-test"]["name"] == "Build Image and Run Tests"
    needs = jobs["build-and-test"]["needs"]
    assert ([needs] if isinstance(needs, str) else list(needs)) == ["run-quality"]
    for job in jobs.values():
        assert job["runs-on"] == "ubuntu-24.04-arm"


def test_quality_job_steps() -> None:
    runs = _runs(_job("run-quality"))
    for expected in (
        "scripts/install_locked.py",
        "ruff check src/ tests/",
        "ruff format --check src/ tests/",
        "pyright src/",
    ):
        assert expected in runs
    assert "docker" not in runs


@pytest.mark.parametrize("job_name", JOBS)
def test_deps_installed_hash_locked_in_both_jobs(job_name: str) -> None:
    runs = _runs(_job(job_name))
    assert "python scripts/install_locked.py" in runs
    assert "poetry==${POETRY_VERSION}" in runs
    env = _load(CI_VALIDATION)["env"]
    assert env["POETRY_VERSION"] == "2.5.1"
    assert env["POETRY_PLUGIN_EXPORT_VERSION"] == "1.10.1"


def test_single_job_build_run_test() -> None:
    job = _job("build-and-test")
    assert len(job["services"]) == 1
    assert next(iter(job["services"].values()))["image"] == "pgvector/pgvector:pg16"
    ids = [s.get("id") for s in _steps(job)]
    order = [
        "build",
        "migrate",
        "run",
        "health",
        "ready",
        "actionlint",
        "unit",
        "integration",
        "teardown",
    ]
    positions = [ids.index(i) for i in order]
    assert positions == sorted(positions)
    build, run, unit, integration = (
        _step("build-and-test", i)["run"] for i in ("build", "run", "unit", "integration")
    )
    assert "docker build --tag sentinel-backend:test" in build
    assert "docker run -d --name sentinel-backend -p 8000:8000" in run
    assert "pytest tests/test_tools/ tests/test_models/ tests/test_scripts/ -x" in unit
    assert "pytest tests/test_agents/ tests/test_api/ tests/test_infra/ -x" in integration
    assert "docker" not in _runs(_job("run-quality"))


def test_jobs_have_timeouts() -> None:
    assert _job("run-quality")["timeout-minutes"] == 20
    assert _job("build-and-test")["timeout-minutes"] == 60


def test_actionlint_is_pinned_and_verified() -> None:
    env = _load(CI_VALIDATION)["env"]
    run = _step("build-and-test", "actionlint")["run"]
    assert env["ACTIONLINT_VERSION"] == "1.7.12"
    assert re.fullmatch(r"[0-9a-f]{64}", env["ACTIONLINT_SHA256"])
    assert "sha256sum -c" in run
    assert "linux_arm64" in run


def test_container_env() -> None:
    job = _job("build-and-test")
    script = _step("build-and-test", "run")["run"]
    assert "SENTINEL_FAKE_LLM=1" in script
    assert 'DATABASE_URL="$CONTAINER_DATABASE_URL"' in script
    assert re.search(r"SENTINEL_API_TENANT_ID=[0-9a-f-]{36}", script)
    assert re.search(r"SENTINEL_API_AUDIENCE=[0-9a-f-]{36}", script)
    assert "host.docker.internal:5432" in job["env"]["CONTAINER_DATABASE_URL"]
    assert job["env"]["TEST_DATABASE_URL"] == "postgresql://postgres:test@localhost:5432/sentinel"
    assert "alembic upgrade head" in _step("build-and-test", "migrate")["run"]


def test_health_poll_is_bounded_then_ready() -> None:
    health = _step("build-and-test", "health")["run"]
    assert "seq 1 30" in health
    assert "http://localhost:8000/health" in health
    ready = _step("build-and-test", "ready")["run"]
    assert "curl -fsS" in ready
    assert "http://localhost:8000/ready" in ready


def test_teardown_always_runs_last() -> None:
    steps = _steps(_job("build-and-test"))
    last = steps[-1]
    assert last["id"] == "teardown"
    assert last["if"] == "always()"
    assert "docker stop sentinel-backend" in last["run"]
    assert "docker rm sentinel-backend" in last["run"]
    assert _step("build-and-test", "logs")["if"] == "failure()"


@pytest.mark.parametrize("path", [CI_VALIDATION, GUARD], ids=["ci_validation", "guard-main-source"])
def test_no_expression_inside_run_blocks(path: Path) -> None:
    for job_name, job in _load(path)["jobs"].items():
        for step in _steps(job):
            if "run" in step:
                assert "${{" not in step["run"], f"{path.name}:{job_name} inlines an expression"


def test_guard_passes_refs_via_env() -> None:
    doc = _load(GUARD)
    job = doc["jobs"]["verify-source-branch"]
    assert job["name"] == "verify-source-branch"
    (step,) = _steps(job)
    assert step["env"] == {"BASE": "${{ github.base_ref }}", "HEAD": "${{ github.head_ref }}"}
    assert _on(doc)["pull_request"]["branches"] == ["main", "release-phase-2"]


@pytest.mark.parametrize(
    ("base", "head", "allowed"),
    [
        ("main", "dev/backend-phase-10-ops-dashboard", True),
        ("main", "fix/guard-post-release", True),
        ("main", "release-phase-2", True),
        ("main", "feature/anything", False),
        ("main", "planning/phase-2-e2e", False),
        ("release-phase-2", "dev/backend-phase-9-cleanup", False),
        ("release-phase-2", "fix/anything", False),
        ("some-other-base", "anything", True),
    ],
)
def test_guard_policy_post_release(base: str, head: str, allowed: bool) -> None:
    """main takes dev/*, fix/* and release-phase-2; release-phase-2 is frozen."""
    _need("bash")
    (step,) = _steps(_load(GUARD)["jobs"]["verify-source-branch"])
    proc = subprocess.run(
        ["bash", "-c", step["run"]],
        env={"PATH": os.environ["PATH"], "BASE": base, "HEAD": head},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (proc.returncode == 0) is allowed, proc.stdout + proc.stderr


def test_actionlint_clean() -> None:
    _need("actionlint")
    proc = subprocess.run(
        ["actionlint", "-no-color", str(CI_VALIDATION), str(GUARD)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
