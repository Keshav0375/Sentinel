"""Static contract of .github/workflows/ops_reconcile.yml (backend task 10.3).

Triggers, the ops-ledger concurrency group, least permissions, a default-branch checkout that never
runs workflow_run code, the pinned and checksum-verified gitleaks, and the `ops-snapshot` artifact
handed to Pages (10.5). The file is read as YAML; nothing here calls GitHub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github/workflows"
WORKFLOW = WORKFLOW_DIR / "ops_reconcile.yml"
STEP_IDS = [
    "checkout",
    "cursor",
    "lf-gate",
    "login",
    "kv",
    "clone",
    "reconcile",
    "refresh",
    "gitleaks-install",
    "gitleaks-ledger",
    "snapshot",
    "gitleaks-snapshot",
    "upload",
]
TRIGGER_WORKFLOWS = [
    "[sentinel] backend — deployment",
    "[sentinel] incident — response pipeline",
    "[sentinel] backend — scale",
    "[sentinel] PR — validation",
    "CI",
    "Runner Image — Build & Deploy to ACR",
    "Sentinel Infra — Deploy",
]
GITLEAKS_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"


def _doc() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    on: dict[str, Any] = doc.get("on", doc.get(True))
    return on


def _job() -> dict[str, Any]:
    job: dict[str, Any] = _doc()["jobs"]["reconcile"]
    return job


def _steps() -> dict[str, dict[str, Any]]:
    return {step["id"]: step for step in _job()["steps"]}


def test_triggers() -> None:
    on = _on(_doc())

    assert [entry["cron"] for entry in on["schedule"]] == ["*/30 * * * *", "17 3 * * 0"]
    assert on["workflow_run"]["workflows"] == TRIGGER_WORKFLOWS
    names = {
        yaml.safe_load(path.read_text(encoding="utf-8"))["name"]
        for path in WORKFLOW_DIR.glob("*.yml")
        if path != WORKFLOW
    }
    for name in TRIGGER_WORKFLOWS:
        assert name in names, name
    assert on["workflow_run"]["types"] == ["requested", "completed"]
    full = on["workflow_dispatch"]["inputs"]["full"]
    assert (full["type"], full["default"]) == ("boolean", False)
    assert set(on) == {"schedule", "workflow_run", "workflow_dispatch"}
    assert not set(on) & {"pull_request", "pull_request_target"}


def test_concurrency_and_permissions() -> None:
    doc = _doc()
    job = _job()

    assert doc["concurrency"] == {"group": "ops-ledger", "cancel-in-progress": False}
    assert doc["permissions"] == {}
    assert list(doc["jobs"]) == ["reconcile"]
    assert job["permissions"] == {"contents": "write", "actions": "read", "id-token": "write"}
    assert job["environment"] == "sentinel-dev"
    assert job["name"] == "Reconcile the ops ledger"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 20


def test_checkout_is_default_branch() -> None:
    checkout = _steps()["checkout"]

    assert str(checkout["uses"]).startswith("actions/checkout@")
    assert set(checkout["with"]) == {"persist-credentials"}
    assert checkout["with"]["persist-credentials"] is False
    assert "workflow_run.head" not in WORKFLOW.read_text(encoding="utf-8")
    gate_env = _steps()["lf-gate"]["env"]
    assert gate_env["TRIGGER_WORKFLOW"] == "${{ github.event.workflow_run.name }}"


def test_steps_order() -> None:
    assert [step["id"] for step in _job()["steps"]] == STEP_IDS


def test_cursor_cache() -> None:
    cursor = _steps()["cursor"]

    assert str(cursor["uses"]).startswith("actions/cache@")
    assert cursor["with"] == {
        "path": "meta/cursor.json",
        "key": "ops-cursor-${{ github.run_id }}-${{ github.run_attempt }}",
        "restore-keys": "ops-cursor-",
    }


def test_artifact_handoff() -> None:
    upload = _steps()["upload"]

    assert upload["uses"] == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    assert upload["with"] == {
        "name": "ops-snapshot",
        "path": "snapshot/",
        "retention-days": 7,
        "if-no-files-found": "error",
    }
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02  # v4.6.2" in text


def test_gitleaks_pinned_and_verified() -> None:
    env = _doc()["env"]
    steps = _steps()

    assert env["GITLEAKS_VERSION"] == "8.30.1"
    assert env["GITLEAKS_SHA256"] == GITLEAKS_SHA256
    install = steps["gitleaks-install"]["run"]
    assert "sha256sum -c" in install
    assert "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" in install
    assert (
        'gitleaks git "$RUNNER_TEMP/ledger" --no-banner --redact' in steps["gitleaks-ledger"]["run"]
    )
    assert "gitleaks dir snapshot --no-banner --redact" in steps["gitleaks-snapshot"]["run"]
    for scan in ("gitleaks-ledger", "gitleaks-snapshot"):
        assert "continue-on-error" not in steps[scan]
        assert "if" not in steps[scan]
        assert STEP_IDS.index(scan) < STEP_IDS.index("upload")


def test_no_expression_in_run() -> None:
    runs = [step for step in _job()["steps"] if "run" in step]

    assert len(runs) == 8
    for step in runs:
        assert "${{" not in step["run"], step["id"]
        assert step["run"].startswith("set -euo pipefail\n"), step["id"]


def test_langfuse_optional() -> None:
    steps = _steps()

    for name in ("login", "kv"):
        assert steps[name]["continue-on-error"] is True
    assert steps["login"]["if"] == "steps.lf-gate.outputs.enabled == 'true'"
    assert steps["kv"]["if"] == "steps.login.outcome == 'success'"
    assert steps["kv"]["uses"] == "./.github/actions/get-kv-secrets"
    assert steps["kv"]["with"]["names"] == (
        "langfuse-public-key,langfuse-secret-key,langfuse-base-url"
    )
    env = steps["reconcile"]["env"]
    assert env["LANGFUSE_PUBLIC_KEY"] == "${{ steps.kv.outputs.langfuse-public-key }}"
    assert env["LANGFUSE_SECRET_KEY"] == "${{ steps.kv.outputs.langfuse-secret-key }}"
    assert env["LANGFUSE_BASE_URL"] == "${{ steps.kv.outputs.langfuse-base-url }}"
    assert env["OPS_FULL_SWEEP"] == "${{ inputs.full }}"
    assert env["OPS_SCHEDULE"] == "${{ github.event.schedule }}"
    gate = steps["lf-gate"]["run"]
    assert "schedule|workflow_dispatch" in gate
    assert '"[sentinel] incident — response pipeline"' in gate


def test_scripts_wired() -> None:
    steps = _steps()

    assert "python3 scripts/ops/reconcile.py run" in steps["reconcile"]["run"]
    assert '--status "$RUNNER_TEMP/ops-status.json"' in steps["reconcile"]["run"]
    snapshot = steps["snapshot"]["run"]
    assert "python3 scripts/ops/snapshot.py" in snapshot
    assert "--out snapshot" in snapshot and '--run-url "$RUN_URL"' in snapshot
    assert "--branch ops-ledger https://github.com/Keshav0375/Sentinel.git" in steps["clone"]["run"]
    assert "pull --quiet --ff-only" in steps["refresh"]["run"]
    assert "setup-python" not in WORKFLOW.read_text(encoding="utf-8")
