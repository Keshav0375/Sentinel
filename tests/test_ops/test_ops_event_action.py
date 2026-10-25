"""The ops-event composite action, run for real under bash (backend task 10.1).

`python3` is either a stub or a shim to this interpreter; the ledger remote is a local bare repo.
Nothing here talks to GitHub.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "ops-event"
INPUT_EXPR = re.compile(r"^\$\{\{ inputs\.([a-z-]+) \}\}$")
WORKFLOW_REF = "Keshav0375/Sentinel/.github/workflows/ci_backend_deployment.yml@refs/heads/main"
SOURCE = "gha:Keshav0375/Sentinel/ci_backend_deployment.yml"
VALID_OUTPUT = "echo \"event-id=$(printf '%064d' 0)\"; echo result=written"

EXPECTED_INPUTS: dict[str, tuple[bool, str | None]] = {
    "kind": (True, None),
    "source-id": (True, None),
    "state": (False, ""),
    "ts": (False, ""),
    "chain": (False, "{}"),
    "data": (False, "{}"),
    "token": (False, "${{ github.token }}"),
}

DEPLOY_INPUTS = {
    "kind": "deploy.succeeded",
    "source-id": "123.1",
    "chain": '{"sha":"abc1234"}',
    "data": (
        '{"run_url":"https://github.com/Keshav0375/Sentinel/actions/runs/123",'
        '"version":"sha-abc1234"}'
    ),
    "token": "",
}


@dataclass
class Run:
    returncode: int
    stdout: str
    outputs: dict[str, str]
    log: list[str]


def _action() -> dict[str, Any]:
    loaded = yaml.safe_load((ACTION_DIR / "action.yml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _step() -> dict[str, Any]:
    steps = _action()["runs"]["steps"]
    assert len(steps) == 1
    return dict(steps[0])


def _run(
    tmp_path: Path,
    inputs: dict[str, str],
    *,
    stubs: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    action_path: Path = ACTION_DIR,
) -> Run:
    assert "token" in inputs, "always pass token explicitly"
    declared: dict[str, Any] = _action()["inputs"]
    step = _step()
    resolved: dict[str, str] = {}
    for key, value in (step.get("env") or {}).items():
        match = INPUT_EXPR.match(str(value))
        assert match, key
        name = match.group(1)
        resolved[key] = inputs[name] if name in inputs else str(declared[name].get("default", ""))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stubs = stubs or {}
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "{name} $*" >> "$STUB_LOG"\n{body}\n')
        path.chmod(0o755)
    if "python3" not in stubs:
        shim = bin_dir / "python3"
        shim.write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
        shim.chmod(0o755)

    runner_temp = tmp_path / "runner"
    runner_temp.mkdir(exist_ok=True)
    github_output = tmp_path / "github-output"
    github_output.unlink(missing_ok=True)
    stub_log = tmp_path / "stub.log"
    stub_log.unlink(missing_ok=True)
    script = tmp_path / "append.sh"
    script.write_text(str(step["run"]))

    run_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GITHUB_OUTPUT": str(github_output),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_ACTION_PATH": str(action_path),
        "STUB_LOG": str(stub_log),
        "GITHUB_REPOSITORY": "Keshav0375/Sentinel",
        "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
        "GITHUB_EVENT_NAME": "push",
        # Never the real ledger: a test that forgets a remote fails on a local path.
        "OPS_LEDGER_REMOTE": (tmp_path / "no-remote.git").as_uri(),
        **resolved,
        **(env or {}),
    }
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    outputs: dict[str, str] = {}
    if github_output.exists():
        for line in github_output.read_text().splitlines():
            key, _, value = line.partition("=")
            outputs[key] = value
    log = stub_log.read_text().splitlines() if stub_log.exists() else []
    return Run(proc.returncode, proc.stdout + proc.stderr, outputs, log)


def test_static_contract() -> None:
    doc = _action()
    assert doc["runs"]["using"] == "composite"
    step = _step()
    assert step["id"] == "append"
    assert step["shell"] == "bash"
    inputs = doc["inputs"]
    assert set(inputs) == set(EXPECTED_INPUTS)
    for name, (required, default) in EXPECTED_INPUTS.items():
        assert bool(inputs[name].get("required", False)) is required, name
        if default is None:
            assert "default" not in inputs[name], name
        else:
            assert str(inputs[name]["default"]) == default, name
    assert doc["outputs"]["event-id"]["value"] == "${{ steps.append.outputs.event-id }}"
    assert doc["outputs"]["result"]["value"] == "${{ steps.append.outputs.result }}"
    assert "contents: write" in doc["description"]
    script = str(step["run"])
    assert "GITHUB_WORKSPACE" not in script
    assert "uses:" not in (ACTION_DIR / "action.yml").read_text()
    assert set(step["env"]) == {
        "OPS_EVENT_KIND",
        "OPS_EVENT_SOURCE_ID",
        "OPS_EVENT_STATE",
        "OPS_EVENT_TS",
        "OPS_EVENT_CHAIN",
        "OPS_EVENT_DATA",
        "OPS_LEDGER_TOKEN",
    }
    assert (ACTION_DIR / ".." / ".." / ".." / "scripts" / "ops" / "ledger.py").resolve().is_file()


def test_writes_to_local_ledger(tmp_path: Path, ledger: ModuleType, remote: Any) -> None:
    env = {"OPS_LEDGER_REMOTE": remote.uri}
    run = _run(tmp_path, DEPLOY_INPUTS, env=env)
    assert run.returncode == 0, run.stdout
    assert run.outputs["result"] == "written", run.stdout
    expected_id = ledger.compute_event_id("deploy.succeeded", SOURCE, "123.1", "")
    assert run.outputs["event-id"] == expected_id
    assert "::warning::" not in run.stdout
    files = remote.files()
    assert {"README.md", "schema/event.v1.json", "schema/snapshot.v1.json"} <= files
    assert len(remote.events()) == 1
    assert remote.events()[0].endswith(f"-deploy.succeeded-{expected_id}.json")

    again = _run(tmp_path, DEPLOY_INPUTS, env=env)
    assert again.returncode == 0, again.stdout
    assert again.outputs == {"event-id": expected_id, "result": "exists"}
    assert len(remote.events()) == 1


def test_invalid_event_warns_and_exits_zero(tmp_path: Path, remote: Any) -> None:
    run = _run(
        tmp_path,
        {**DEPLOY_INPUTS, "kind": "deploy.exploded"},
        env={"OPS_LEDGER_REMOTE": remote.uri},
    )
    assert run.returncode == 0
    assert run.outputs == {"result": "failed"}
    assert "::warning::ops-event:" in run.stdout
    assert remote.git("ls-remote", "--heads", str(remote.path)) == ""


def test_ledger_failure_never_fails_caller(tmp_path: Path) -> None:
    run = _run(tmp_path, DEPLOY_INPUTS, stubs={"python3": "exit 3"})
    assert run.returncode == 0
    assert run.outputs == {"result": "failed"}
    assert "::warning::ops-event: not recorded (ledger exit 3)" in run.stdout
    assert len(run.log) == 1
    assert run.log[0].startswith("python3 ")
    assert run.log[0].endswith("/scripts/ops/ledger.py append")


def test_source_derived_from_workflow_ref(tmp_path: Path) -> None:
    body = 'printf \'SOURCE=%s\\n\' "$OPS_EVENT_SOURCE" >> "$STUB_LOG"; ' + VALID_OUTPUT
    run = _run(tmp_path, DEPLOY_INPUTS, stubs={"python3": body})
    assert run.returncode == 0, run.stdout
    assert run.outputs == {"event-id": "0" * 64, "result": "written"}
    assert f"SOURCE={SOURCE}" in run.log
    assert "ops-event: recorded" in run.stdout


def test_unexpected_ledger_output_is_not_trusted(tmp_path: Path) -> None:
    run = _run(tmp_path, DEPLOY_INPUTS, stubs={"python3": "echo event-id=xyz; echo evil=1"})
    assert run.returncode == 0
    assert run.outputs == {"result": "failed"}
    assert "evil" not in run.outputs
    assert "::warning::ops-event:" in run.stdout


@pytest.mark.parametrize("event_name", ["pull_request", "pull_request_target"])
def test_pull_request_events_skipped(tmp_path: Path, event_name: str) -> None:
    run = _run(
        tmp_path,
        DEPLOY_INPUTS,
        stubs={"python3": VALID_OUTPUT},
        env={"GITHUB_EVENT_NAME": event_name},
    )
    assert run.returncode == 0
    assert run.log == []
    assert run.outputs == {"result": "skipped"}
    assert "::warning::ops-event: skipped" in run.stdout


def test_token_masked(tmp_path: Path) -> None:
    run = _run(tmp_path, {**DEPLOY_INPUTS, "token": "t0k3n"}, stubs={"python3": VALID_OUTPUT})
    assert run.returncode == 0, run.stdout
    b64 = base64.b64encode(b"x-access-token:t0k3n").decode()
    assert "::add-mask::t0k3n" in run.stdout
    assert f"::add-mask::{b64}" in run.stdout
    assert run.stdout.index("::add-mask::") < run.stdout.index("ops-event: recorded")


def test_missing_ledger_warns(tmp_path: Path) -> None:
    copy = tmp_path / "x" / "y" / "z" / "ops-event"
    shutil.copytree(ACTION_DIR, copy)
    run = _run(tmp_path, DEPLOY_INPUTS, stubs={"python3": VALID_OUTPUT}, action_path=copy)
    assert run.returncode == 0
    assert run.outputs == {"result": "failed"}
    assert "::warning::ops-event: not recorded: scripts/ops/ledger.py is missing" in run.stdout
    assert run.log == []
