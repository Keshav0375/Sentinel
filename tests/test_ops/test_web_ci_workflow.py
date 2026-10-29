"""Static contract of .github/workflows/ci_web.yml and the Lighthouse budget (backend task 10.5).

Path-filtered PR/push triggers, cloud-free permissions, Node 22 + Corepack pnpm, the step order
(site check before e2e, fixtures staged only for Lighthouse), the Playwright browser cache and the
performance/accessibility budget in dashboard-web/lighthouserc.json. Nothing here runs a browser.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WF = REPO_ROOT / ".github/workflows"
WORKFLOW = WF / "ci_web.yml"
WEB = REPO_ROOT / "dashboard-web"
# check_site.py validates against scripts/ops/schema/, so a schema change reruns web CI.
PATHS = [
    "dashboard-web/**",
    "scripts/ops/check_site.py",
    "scripts/ops/schema/**",
    ".github/workflows/ci_web.yml",
]
PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-node": "949feb2413d6458794dcd2491c4babbbce0c15c1",
    "actions/cache": "0400d5f644dc74513175e3cd8d07132dd4860809",
}
STEPS = [
    "Check out",
    "Set up Node",
    "Enable pnpm (Corepack)",
    "Locate the pnpm store",
    "Cache the pnpm store",
    "Install",
    "Typecheck",
    "Lint",
    "Unit tests",
    "Build",
    "Check the build (base path, no fixtures)",
    "Cache Playwright browsers",
    "Install Playwright Chromium",
    "Install Playwright system dependencies",
    "End-to-end tests (Playwright)",
    "Stage the fixture snapshot for Lighthouse",
    "Lighthouse CI (performance and accessibility ≥ 90)",
]
BUDGET = ["error", {"minScore": 0.9, "aggregationMethod": "median-run"}]
LIGHTHOUSE_SCRIPT = (
    "lhci collect --config=./lighthouserc.json && lhci assert --config=./lighthouserc.json"
)


def _doc(name: str = "ci_web.yml") -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    return doc


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    on: dict[str, Any] = doc.get("on", doc.get(True))
    return on


def _steps(doc: dict[str, Any], job: str = "web") -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = doc["jobs"][job]["steps"]
    return steps


def _step(name: str) -> dict[str, Any]:
    return next(s for s in _steps(_doc()) if s["name"] == name)


def _uses(prefix: str) -> list[dict[str, Any]]:
    return [s for s in _steps(_doc()) if str(s.get("uses", "")).startswith(prefix)]


def test_triggers_path_filtered() -> None:
    on = _on(_doc())

    assert on["pull_request"] == {"branches": ["main"], "paths": PATHS}
    assert on["push"] == {"branches": ["main"], "paths": PATHS}
    assert "workflow_dispatch" in on
    assert set(on) == {"pull_request", "push", "workflow_dispatch"}
    assert not set(on) & {"pull_request_target", "workflow_run", "schedule"}


def test_cloud_free() -> None:
    doc = _doc()
    # The parsed workflow, not the raw file: the header comment says "no id-token, no secrets".
    text = yaml.safe_dump(doc, allow_unicode=True)

    assert doc["permissions"] == {"contents": "read"}
    for job_id, job in doc["jobs"].items():
        assert "permissions" not in job, job_id
        assert "environment" not in job, job_id
    for needle in ("id-token", "secrets.", "azure/login", "vars."):
        assert needle not in text, needle


def test_concurrency() -> None:
    assert _doc()["concurrency"] == {
        "group": "ci-web-${{ github.ref }}",
        "cancel-in-progress": True,
    }


def test_node_and_pnpm() -> None:
    doc = _doc()
    job = doc["jobs"]["web"]
    (setup,) = _uses("actions/setup-node@")
    version = tuple(int(x) for x in (WEB / ".node-version").read_text().strip().split("."))
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))

    assert setup["with"] == {
        "node-version-file": "dashboard-web/.node-version",
        "package-manager-cache": False,
    }
    assert version[0] == 22
    assert version >= (22, 22, 2)
    assert package["packageManager"] == "pnpm@10.34.9"
    assert job["defaults"]["run"]["working-directory"] == "dashboard-web"
    assert doc["defaults"] == {"run": {"shell": "bash"}}
    assert doc["env"] == {"COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 30
    assert "corepack enable" in _step("Enable pnpm (Corepack)")["run"]
    assert "pnpm install --frozen-lockfile" in _step("Install")["run"]


def test_step_order() -> None:
    assert [s["name"] for s in _steps(_doc())] == STEPS


def test_playwright_browser_cache() -> None:
    cache = _step("Cache Playwright browsers")
    install = _step("Install Playwright Chromium")
    deps = _step("Install Playwright system dependencies")

    assert cache["id"] == "pw-cache"
    assert cache["with"]["path"] == "~/.cache/ms-playwright"
    assert "hashFiles('dashboard-web/pnpm-lock.yaml')" in cache["with"]["key"]
    assert install["if"] == "steps.pw-cache.outputs.cache-hit != 'true'"
    assert "playwright install --with-deps chromium" in install["run"]
    assert deps["if"] == "steps.pw-cache.outputs.cache-hit == 'true'"
    assert "playwright install-deps chromium" in deps["run"]
    assert "pnpm run e2e" in _step("End-to-end tests (Playwright)")["run"]


def test_site_check_before_fixture_staging() -> None:
    names = [s["name"] for s in _steps(_doc())]
    check = _step("Check the build (base path, no fixtures)")
    stage = _step("Stage the fixture snapshot for Lighthouse")

    assert check["working-directory"] == "."
    assert "python3 scripts/ops/check_site.py dashboard-web/dist" in check["run"]
    assert "--base /Sentinel/" in check["run"]
    assert "--fixtures dashboard-web/src/fixtures" in check["run"]
    assert (
        names.index(check["name"])
        < names.index("End-to-end tests (Playwright)")
        < names.index(stage["name"])
        < names.index("Lighthouse CI (performance and accessibility ≥ 90)")
    )
    assert "cp -R src/fixtures/populated/. dist/snapshot/" in stage["run"]
    assert (
        "pnpm run lighthouse" in _step("Lighthouse CI (performance and accessibility ≥ 90)")["run"]
    )


def test_lighthouse_budget() -> None:
    rc = json.loads((WEB / "lighthouserc.json").read_text(encoding="utf-8"))["ci"]
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    ignored = (WEB / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert rc["collect"]["url"] == ["http://localhost:4173/Sentinel/"]
    assert rc["collect"]["startServerCommand"] == "pnpm run preview"
    assert rc["collect"]["numberOfRuns"] == 5
    assert rc["assert"]["assertions"] == {
        "categories:performance": BUDGET,
        "categories:accessibility": BUDGET,
    }
    assert package["devDependencies"]["@lhci/cli"] == "0.15.1"
    assert package["scripts"]["lighthouse"] == LIGHTHOUSE_SCRIPT
    assert ".lighthouseci/" in ignored


def test_pins() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = {s["uses"] for s in _steps(_doc()) if "uses" in s}

    assert uses == {f"{action}@{sha}" for action, sha in PINS.items()}
    for action, sha in PINS.items():
        assert f"uses: {action}@{sha}  # v" in text, action


def test_no_expression_in_run() -> None:
    runs = [s["run"] for s in _steps(_doc()) if "run" in s]

    assert runs
    for run in runs:
        assert "${{" not in run, run
        assert run.splitlines()[0] == "set -euo pipefail", run


def test_actionlint_clean() -> None:
    if shutil.which("actionlint") is None:
        pytest.skip("actionlint not on PATH")
    proc = subprocess.run(
        ["actionlint", "-no-color", str(WORKFLOW)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
