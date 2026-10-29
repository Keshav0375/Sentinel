"""scripts/ops/emit.py as the workflows run it: stdout is GITHUB_OUTPUT (backend task 10.2).

Each test runs the script with a scrubbed environment (only what a runner would set) and a temp
event file. Nothing here touches git or the network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EMIT = REPO_ROOT / "scripts/ops/emit.py"
SHA = "4f1d2a9c0b8e7d6f5a4b3c2d1e0f9a8b7c6d5e4f"
FIELDS = ("kind", "source-id", "state", "chain", "data")


@pytest.fixture(scope="session")
def incident_dispatch() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "emit_incident_dispatch", REPO_ROOT / "scripts/incident_dispatch.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_emit(
    command: str, env: dict[str, str] | None = None, *, base: bool = True
) -> tuple[int, dict[str, str], list[str], str]:
    """(rc, outputs, raw stdout lines, stderr) of one emit.py run."""
    full_env = {"PATH": os.environ.get("PATH", "")}
    if base:
        full_env.update(
            {
                "GITHUB_REPOSITORY": "Keshav0375/Sentinel",
                "GITHUB_RUN_ID": "7",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_SHA": SHA,
            }
        )
    full_env.update(env or {})
    proc = subprocess.run(
        [sys.executable, str(EMIT), command],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = proc.stdout.splitlines()
    outputs: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        assert sep, f"not a GITHUB_OUTPUT line: {line!r}"
        outputs[key] = value
    return proc.returncode, outputs, lines, proc.stderr


def _event_file(tmp_path: Path, client_payload: dict[str, Any]) -> str:
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"action": "incident-alert", "client_payload": client_payload}))
    return str(path)


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "signal_type": "runtime_error",
        "correlation_id": "evt-1",
        "event": {
            "title": "Error rate high",
            "severity": "P2",
            "tags": ["service:sentinel-watchtower"],
        },
    }
    payload.update(overrides)
    return payload


def test_backend_start_outputs() -> None:
    rc, outputs, lines, _ = run_emit("backend-start")
    assert rc == 0
    assert lines[0] == "build-kind=build.started"
    assert lines[1] == "build-source-id=7.1"
    assert lines[2] == "build-state="
    assert [line.partition("=")[0] for line in lines] == [f"build-{f}" for f in FIELDS]
    assert json.loads(outputs["build-chain"]) == {"sha": SHA, "run_id": 7}
    assert json.loads(outputs["build-data"]) == {
        "version": "sha-4f1d2a9",
        "run_url": "https://github.com/Keshav0375/Sentinel/actions/runs/7",
    }
    assert outputs["build-chain"] == f'{{"sha":"{SHA}","run_id":7}}'


def test_backend_deploy_outputs_empty_slot() -> None:
    rc, outputs, lines, _ = run_emit(
        "backend-deploy",
        {
            "QUALITY_RESULT": "failure",
            "BUILD_RESULT": "skipped",
            "DEPLOY_RESULT": "skipped",
            "TESTS_RESULT": "skipped",
        },
    )
    assert rc == 0
    assert outputs["build-kind"] == "build.failed"
    assert json.loads(outputs["build-data"])["failed_stage"] == "quality"
    assert len(lines) == 10
    for field in FIELDS:
        assert outputs[f"deploy-{field}"] == ""


def test_incident_start_from_event_file(tmp_path: Path, incident_dispatch: ModuleType) -> None:
    rc, outputs, _, _ = run_emit(
        "incident-start", {"GITHUB_EVENT_PATH": _event_file(tmp_path, _payload())}
    )
    expected = incident_dispatch.correlation_uuid("evt-1")
    assert rc == 0
    assert outputs["alert-kind"] == "alert.received"
    assert outputs["alert-state"] == "runtime_error"
    assert outputs["alert-source-id"] == expected
    assert json.loads(outputs["alert-chain"]) == {"correlation_id": expected}
    assert json.loads(outputs["alert-data"]) == {
        "signal_type": "runtime_error",
        "service": "sentinel-watchtower",
        "severity": "P2",
    }
    assert outputs["agent-kind"] == "agent.started"
    assert outputs["agent-source-id"] == expected


def test_incident_missing_correlation_is_quiet(tmp_path: Path) -> None:
    payload = _payload()
    del payload["correlation_id"]
    for command in ("incident-start", "incident-result"):
        rc, outputs, lines, stderr = run_emit(
            command, {"GITHUB_EVENT_PATH": _event_file(tmp_path, payload)}
        )
        assert rc == 0
        kinds = {k: v for k, v in outputs.items() if k.endswith("-kind")}
        assert len(kinds) == 2 and set(kinds.values()) == {""}
        assert "::warning::ops-emit:" in stderr
        assert not any(line.startswith("::") for line in lines)


def test_incident_missing_event_file_is_quiet() -> None:
    rc, outputs, _, stderr = run_emit("incident-start", {"GITHUB_EVENT_PATH": "/nonexistent"})
    assert rc == 0
    assert outputs["alert-kind"] == "" and outputs["agent-kind"] == ""
    assert "::warning::ops-emit:" in stderr


def _result_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "GITHUB_EVENT_PATH": _event_file(tmp_path, _payload()),
        "ENSURE_RESULT": "success",
        "PIPELINE_RESULT": "success",
        "RESOLUTION_TYPE": "rollback",
        "CONFIDENCE": "0.91",
        "ROOT_CAUSE": "bad config",
        "EVIDENCE": "logs show errors",
        "INCIDENT_ID": "0b6c3d2e-1f4a-4b5c-8d9e-0a1b2c3d4e5f",
        "PR_DETAILS": json.dumps({"number": 3, "sha": SHA, "branch": "runtime/04"}),
    }
    env.update(overrides)
    return env


def test_incident_result_outputs(tmp_path: Path) -> None:
    rc, outputs, lines, _ = run_emit("incident-result", _result_env(tmp_path))
    assert rc == 0
    assert len(lines) == 10
    assert outputs["decision-kind"] == "agent.decided"
    assert outputs["decision-state"] == "rollback"
    assert outputs["escalation-kind"] == ""
    data = json.loads(outputs["decision-data"])
    assert data["confidence"] == 0.91
    assert data["root_cause_summary"] == "bad config"
    chain = json.loads(outputs["decision-chain"])
    assert chain["sha"] == SHA and chain["scenario"] == "runtime/04"

    rc, outputs, _, _ = run_emit(
        "incident-result", _result_env(tmp_path, RESOLUTION_TYPE="escalated")
    )
    assert outputs["escalation-kind"] == "escalation.raised"


def test_incident_result_failure(tmp_path: Path) -> None:
    rc, outputs, _, _ = run_emit(
        "incident-result",
        _result_env(tmp_path, ENSURE_RESULT="failure", PIPELINE_RESULT="skipped", PR_DETAILS=""),
    )
    assert rc == 0
    assert outputs["decision-kind"] == "agent.failed"
    assert json.loads(outputs["decision-data"])["error_class"] == "backend_unavailable"


def test_scale_outputs() -> None:
    rc, outputs, _, _ = run_emit(
        "scale", {"GITHUB_EVENT_NAME": "schedule", "SCALE_RESULT": "success", "KEEP_WARM": ""}
    )
    assert rc == 0
    assert outputs["scale-kind"] == "scale.changed"
    assert outputs["scale-state"] == "0"
    assert json.loads(outputs["scale-data"])["replicas"] == 0
    assert json.loads(outputs["scale-chain"]) == {"run_id": 7}

    _, outputs, _, _ = run_emit(
        "scale",
        {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "DIRECTION": "down",
            "SCALE_RESULT": "success",
            "KEEP_WARM": "true",
        },
    )
    assert outputs["scale-kind"] == ""


@pytest.mark.parametrize("name", ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"])
def test_bad_run_id_is_quiet(name: str) -> None:
    rc, outputs, lines, stderr = run_emit("backend-start", {name: "x"})
    assert rc == 0
    assert outputs["build-kind"] == ""
    assert len(lines) == 5
    assert f"::warning::ops-emit: {name} is not an integer" in stderr


def test_other_repository_is_quiet() -> None:
    rc, outputs, _, stderr = run_emit("backend-start", {"GITHUB_REPOSITORY": "someone/Sentinel"})
    assert rc == 0
    assert outputs["build-kind"] == ""
    assert "::warning::ops-emit:" in stderr
    assert "someone" not in stderr


def test_unbuildable_event_empties_the_slot() -> None:
    rc, outputs, _, stderr = run_emit("backend-start", {"GITHUB_SHA": "not-a-sha"})
    assert rc == 0
    assert outputs["build-kind"] == ""
    assert "::warning::ops-emit: build event not built (SchemaValidationError)" in stderr
    assert "not-a-sha" not in stderr


def test_never_prints_secret(tmp_path: Path) -> None:
    token = "gh" + "p_" + "A" * 36
    rc, outputs, lines, stderr = run_emit(
        "incident-result",
        _result_env(tmp_path, RESOLUTION_TYPE="escalated", ROOT_CAUSE=f"leaked {token} here"),
    )
    assert rc == 0
    joined = "\n".join(lines)
    assert token not in joined
    assert token not in stderr
    assert outputs["decision-kind"] == "agent.decided"
    assert "root_cause_summary" not in json.loads(outputs["decision-data"])
    assert outputs["escalation-kind"] == "escalation.raised"
    assert "summary" not in json.loads(outputs["escalation-data"])
    assert "root_cause_summary dropped" in stderr


def test_infra_runners_outputs() -> None:
    rc, outputs, _, _ = run_emit("infra-runners", {"BUILD_RESULT": "failure"})
    assert rc == 0
    assert outputs["build-kind"] == "build.failed"
    assert json.loads(outputs["build-chain"]) == {"run_id": 7}
    assert json.loads(outputs["build-data"])["failed_stage"] == "build"
    _, outputs, _, _ = run_emit("infra-runners-start")
    assert outputs["build-kind"] == "build.started"


def test_infra_deploy_outputs() -> None:
    rc, outputs, _, _ = run_emit(
        "infra-deploy",
        {"RUN_RESULT": "success", "VERIFY_RESULT": "success", "GUARDS_PASSED": "true"},
    )
    assert rc == 0
    assert outputs["deploy-kind"] == "deploy.succeeded"
    assert json.loads(outputs["deploy-chain"]) == {"run_id": 7}

    _, outputs, _, _ = run_emit(
        "infra-deploy", {"RUN_RESULT": "failure", "VERIFY_RESULT": "skipped", "GUARDS_PASSED": ""}
    )
    assert outputs["deploy-kind"] == "deploy.failed"
    assert json.loads(outputs["deploy-data"])["failed_stage"] == "guards"

    _, outputs, _, _ = run_emit("infra-deploy-start")
    assert outputs["deploy-kind"] == "deploy.started"
