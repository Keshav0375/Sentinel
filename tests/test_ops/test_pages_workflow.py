"""Static contract of .github/workflows/ops_pages.yml (backend task 10.5).

Triggers (reconcile + web CI on main, or by hand), least privilege with pages/id-token only on the
deploy job, the run gate that keeps PR runs from deploying, a default-branch checkout, mutually
exclusive snapshot sources and the first-run / empty-site guard. The ledger-state step is run
against a stubbed git; nothing here calls GitHub.
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
WF = REPO_ROOT / ".github/workflows"
WORKFLOW = WF / "ops_pages.yml"
RECONCILE_PATH = ".github/workflows/ops_reconcile.yml"
ALLOWED_EVENTS = ["push", "schedule", "workflow_run", "workflow_dispatch"]
PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-node": "949feb2413d6458794dcd2491c4babbbce0c15c1",
    "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
    "actions/upload-pages-artifact": "fc324d3547104276b827a68afc52ff2a11cc49c9",
    "actions/configure-pages": "45bfe0192ca1faeb007ade9deae92b16b8254a0d",
    "actions/deploy-pages": "368f82528645a54fb793d4d04e342629a3f51346",
}
VERSIONS = {
    "actions/checkout": "v4.4.0",
    "actions/setup-node": "v7.1.0",
    "actions/download-artifact": "v8.0.2",
    "actions/upload-pages-artifact": "v5.0.0",
    "actions/configure-pages": "v6.0.0",
    "actions/deploy-pages": "v5.0.1",
}
BUILD_STEPS = [
    "Check out",
    "Set up Node",
    "Enable pnpm (Corepack)",
    "Install",
    "Build",
    "Check whether the ledger exists",
    "Download the reconciled snapshot",
    "Rebuild the snapshot from the ledger",
    "Check the site before publishing",
    "Upload the Pages artifact",
]


def _doc(name: str = "ops_pages.yml") -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    return doc


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    on: dict[str, Any] = doc.get("on", doc.get(True))
    return on


def _steps(doc: dict[str, Any], job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = doc["jobs"][job]["steps"]
    return steps


def _step(name: str, job: str = "build") -> dict[str, Any]:
    return next(s for s in _steps(_doc(), job) if s["name"] == name)


def _by_id(step_id: str) -> dict[str, Any]:
    return next(s for s in _steps(_doc(), "build") if s.get("id") == step_id)


def _flat(value: str) -> str:
    return " ".join(value.split())


def _all_runs() -> list[str]:
    doc = _doc()
    return [s["run"] for job in doc["jobs"] for s in _steps(doc, job) if "run" in s]


def test_triggers() -> None:
    on = _on(_doc())

    assert set(on) == {"workflow_run", "workflow_dispatch"}
    reconcile = _doc("ops_reconcile.yml")["name"]
    web = _doc("ci_web.yml")["name"]
    assert on["workflow_run"] == {
        "workflows": [reconcile, web],
        "types": ["completed"],
        "branches": ["main"],
    }
    assert reconcile == "[ops] reconcile — ledger and snapshot"
    assert web == "[ops] dashboard — web CI"
    assert not set(on) & {"push", "pull_request", "pull_request_target", "schedule"}


def test_permissions_least_privilege() -> None:
    doc = _doc()
    jobs = doc["jobs"]

    assert doc["permissions"] == {}
    assert list(jobs) == ["build", "deploy"]
    assert jobs["build"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    for job_id, job in jobs.items():
        if job_id == "deploy":
            continue
        perms = job.get("permissions") or {}
        assert "id-token" not in perms, job_id
        assert perms.get("pages") != "write", job_id
    assert jobs["deploy"]["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deploy.outputs.page_url }}",
    }
    assert "environment" not in jobs["build"]


def test_only_this_workflow_deploys_pages() -> None:
    others = [p for p in sorted(WF.glob("*.yml")) if p != WORKFLOW]

    assert others
    for path in others:
        text = path.read_text(encoding="utf-8")
        for needle in (
            "pages: write",
            "github-pages",
            "actions/deploy-pages@",
            "actions/upload-pages-artifact@",
        ):
            assert needle not in text, f"{path.name}: {needle}"


def test_concurrency() -> None:
    assert _doc()["concurrency"] == {"group": "pages", "cancel-in-progress": False}


def test_build_gate() -> None:
    jobs = _doc()["jobs"]
    condition = _flat(jobs["build"]["if"])

    assert "github.event.workflow_run.conclusion == 'success'" in condition
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in condition
    assert (
        "(github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main')" in condition
    )
    match = re.search(r"fromJSON\('(\[.*?\])'\), github\.event\.workflow_run\.event\)", condition)
    assert match is not None, condition
    events = json.loads(match.group(1))
    assert events == ALLOWED_EVENTS
    assert not [e for e in events if e.startswith("pull_request")]
    assert jobs["deploy"]["needs"] == "build"
    assert "if" not in jobs["deploy"]


def test_step_order() -> None:
    doc = _doc()

    assert [s["name"] for s in _steps(doc, "build")] == BUILD_STEPS
    assert [s["name"] for s in _steps(doc, "deploy")] == ["Configure Pages", "Deploy"]
    assert [s.get("with") for s in _steps(doc, "deploy")] == [None, None]
    assert _step("Deploy", "deploy")["id"] == "deploy"


def test_pins() -> None:
    doc = _doc()
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = {s["uses"] for job in doc["jobs"] for s in _steps(doc, job) if "uses" in s}

    assert uses == {f"{action}@{sha}" for action, sha in PINS.items()}
    for action, sha in PINS.items():
        assert f"uses: {action}@{sha}  # {VERSIONS[action]}" in text, action


def test_checkout_default_branch() -> None:
    checkout = _step("Check out")
    text = WORKFLOW.read_text(encoding="utf-8")

    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"persist-credentials": False}
    for needle in ("workflow_run.head_sha", "workflow_run.head_branch", "head.ref"):
        assert needle not in text, needle


def test_snapshot_sources_are_exclusive() -> None:
    artifact = _by_id("snapshot-artifact")
    rebuild = _by_id("snapshot-ledger")

    assert artifact["if"] == f"github.event.workflow_run.path == '{RECONCILE_PATH}'"
    assert rebuild["if"] == f"github.event.workflow_run.path != '{RECONCILE_PATH}'"
    assert artifact["uses"].startswith("actions/download-artifact@")
    assert artifact["with"] == {
        "name": "ops-snapshot",
        "path": "dashboard-web/dist/snapshot",
        "run-id": "${{ github.event.workflow_run.id }}",
        "github-token": "${{ github.token }}",
    }
    reconcile = _doc("ops_reconcile.yml")
    upload = next(
        s
        for s in _steps(reconcile, "reconcile")
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert upload["with"]["name"] == "ops-snapshot"
    assert (REPO_ROOT / RECONCILE_PATH).is_file()


def test_first_run_and_empty_guard() -> None:
    state = _by_id("ledger-state")["run"]
    rebuild = _by_id("snapshot-ledger")
    check = _by_id("check")
    names = [s["name"] for s in _steps(_doc(), "build")]

    assert "--exit-code" in state
    assert re.search(r"^\s*2\) echo \"exists=false\"", state, re.MULTILINE)
    assert re.search(r"^\s*\*\) .*exit 1 ;;", state, re.MULTILINE)
    run = rebuild["run"]
    assert rebuild["env"] == {"LEDGER_EXISTS": "${{ steps.ledger-state.outputs.exists }}"}
    else_branch = run.split("else", 1)[1].split("fi", 1)[0]
    assert 'mkdir -p "$ledger"' in else_branch
    assert "snapshot.py --ledger" in run
    assert "--branch ops-ledger" in run
    assert check["env"] == {"LEDGER_EXISTS": "${{ steps.ledger-state.outputs.exists }}"}
    assert "--snapshot" in check["run"]
    assert 'if [ "$LEDGER_EXISTS" = "true" ]; then args+=(--require-events); fi' in check["run"]
    assert names.index("Check the site before publishing") < names.index(
        "Upload the Pages artifact"
    )
    assert _step("Upload the Pages artifact")["with"] == {"path": "dashboard-web/dist/"}


@pytest.mark.parametrize(
    ("stub_rc", "rc", "line"),
    [("0", 0, "exists=true"), ("2", 0, "exists=false"), ("128", 1, None)],
)
def test_ledger_state_behaviour(tmp_path: Path, stub_rc: str, rc: int, line: str | None) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    run = _by_id("ledger-state")["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("timeout", 'shift; exec "$@"'), ("git", 'exit "$STUB_RC"')):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "STUB_RC": stub_rc,
        "GITHUB_OUTPUT": str(output),
        "LEDGER_REMOTE": "https://example.invalid/ledger.git",
    }

    proc = subprocess.run(
        [bash, "-c", run], env=env, capture_output=True, text=True, timeout=30, check=False
    )

    assert proc.returncode == rc, proc.stdout + proc.stderr
    lines = output.read_text(encoding="utf-8").splitlines()
    if line is None:
        assert not [x for x in lines if x.startswith("exists=")]
        assert "the published site is unchanged" in proc.stdout
    else:
        assert lines == [line]


def test_no_expression_in_run() -> None:
    runs = _all_runs()

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
