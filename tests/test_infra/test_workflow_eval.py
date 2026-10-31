"""ci_eval.yml: the in-cluster eval workflow (decision 2026-10-28, eval runs in-cluster).

Dispatch only, serialised with every backend workflow, gated on the preflight. The eval job brings
the backend up, pauses it (the quota fits one pod), runs the eval Job from this checkout and reads
the reports from its logs; teardown deletes the Job and restores the backend per keep-warm; the ops
job is the sole writer of eval.scored. Static tests read the workflow as YAML; the inputs step runs
under the local bash. Nothing here calls GitHub or Azure.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/ci_eval.yml"
MANIFEST = REPO_ROOT / "azure/k8s/jobs/eval-job.yaml"
OPS_DIR = REPO_ROOT / "scripts/ops"
GATE = "needs.preflight.outputs.configured == 'true'"
AKS_RESOURCE = "6dae42f8-4368-4678-94ff-3960e28e3630"
EVAL_STEPS = [
    "checkout",
    "inputs",
    "login",
    "resolve-tag",
    "up",
    "pause",
    "refresh-login",
    "job",
    "collect",
    "upload",
    "verdict",
]
FORBIDDEN = ("get-kv-secrets", "anthropic-api-key", "openai-api-key", "secrets.")
PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_]+)\}")
INPUTS_CTX = re.compile(r"(?<![\w.-])inputs\.")


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _doc() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(_text())
    return doc


def _jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = _doc()["jobs"]
    return jobs


def _steps(job_id: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _jobs()[job_id]["steps"]
    return steps


def _step(job_id: str, step_id: str) -> dict[str, Any]:
    (step,) = [s for s in _steps(job_id) if s.get("id") == step_id]
    return step


def _if(value: Any) -> str:
    return " ".join(str(value or "").split())


def _emit() -> ModuleType:
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    spec = importlib.util.spec_from_file_location("eval_wf_emit", OPS_DIR / "emit.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eval_wf_emit"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── static ────────────────────────────────────────────────────────────────────


def test_name_trigger_concurrency_permissions() -> None:
    doc = _doc()
    assert doc["name"] == "[sentinel] eval — scenario scoring"
    on = doc.get("on", doc.get(True))
    assert on == {
        "workflow_dispatch": {
            "inputs": {
                "dataset": {
                    "description": "LangFuse dataset to score",
                    "required": False,
                    "type": "string",
                    "default": "sentinel-scenario-runs",
                },
                "run-label": {
                    "description": "Optional run-name prefix ([a-z0-9-], max 40)",
                    "required": False,
                    "type": "string",
                    "default": "",
                },
            }
        }
    }
    assert doc["concurrency"] == {"group": "sentinel-backend", "cancel-in-progress": False}
    assert doc["permissions"] == {"id-token": "write", "contents": "read"}


def test_job_order_and_gates() -> None:
    jobs = _jobs()
    assert list(jobs) == ["preflight", "eval", "teardown-backend", "ops-events"]
    assert _if(jobs["eval"]["if"]) == GATE
    assert _if(jobs["teardown-backend"]["if"]) == f"always() && {GATE}"
    assert _if(jobs["ops-events"]["if"]) == f"always() && {GATE}"
    for job_id, job in jobs.items():
        assert job["runs-on"] == "ubuntu-24.04", job_id
        if job_id != "preflight":
            assert job["environment"] == "sentinel-dev", job_id
    assert jobs["teardown-backend"]["needs"] == ["preflight", "eval"]
    assert jobs["ops-events"]["needs"] == ["preflight", "eval"]


def test_eval_steps_in_order() -> None:
    job = _jobs()["eval"]
    assert [s["id"] for s in job["steps"]] == EVAL_STEPS
    assert job["outputs"] == {
        "image-tag": "${{ steps.resolve-tag.outputs.image-tag }}",
        "paused": "${{ steps.pause.outputs.paused }}",
    }
    assert job["timeout-minutes"] == 120
    assert _step("eval", "resolve-tag")["with"] == {"acr-name": "${{ vars.ACR_NAME }}"}
    assert "if" not in _step("eval", "up")


def test_backend_up_callers_pass_redis_and_tag() -> None:
    up = _step("eval", "up")
    warm = _step("teardown-backend", "restore-warm")
    assert up["uses"] == warm["uses"] == "./.github/actions/backend-up"
    assert up["with"]["image-tag"] == "${{ steps.resolve-tag.outputs.image-tag }}"
    assert warm["with"]["image-tag"] == "${{ needs.eval.outputs.image-tag }}"
    rest_up = {k: v for k, v in up["with"].items() if k != "image-tag"}
    rest_warm = {k: v for k, v in warm["with"].items() if k != "image-tag"}
    assert rest_up == rest_warm
    assert rest_up["redis-host"] == "${{ vars.REDIS_HOST }}"
    assert rest_up["redis-port"] == "${{ vars.REDIS_PORT }}"
    scale = yaml.safe_load((REPO_ROOT / ".github/workflows/ci_backend_scale.yml").read_text())
    (scale_up,) = [s for s in scale["jobs"]["scale"]["steps"] if s.get("id") == "up"]
    assert up["with"] == scale_up["with"]


def test_no_expression_in_run_and_inputs_via_env() -> None:
    for job_id, job in _jobs().items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), f"{job_id}:{step.get('id')}"
    inputs = _step("eval", "inputs")
    assert inputs["env"] == {
        "DATASET": "${{ inputs.dataset }}",
        "RUN_LABEL": "${{ inputs.run-label }}",
    }
    # The `inputs` context only; `steps.inputs.outputs.*` is the checked step's output.
    uses = [line for line in _text().splitlines() if INPUTS_CTX.search(line)]
    assert uses == [
        "          DATASET: ${{ inputs.dataset }}",
        "          RUN_LABEL: ${{ inputs.run-label }}",
    ]


def test_no_llm_or_kv_secret_path() -> None:
    text = _text()
    for needle in FORBIDDEN:
        assert needle not in text, needle


def test_pause_records_before_scaling() -> None:
    step = _step("eval", "pause")
    run = step["run"]
    assert step["name"] == "Pause the backend (the quota fits one pod)"
    assert run.startswith("set -euo pipefail")
    assert run.index('echo "paused=true" >> "$GITHUB_OUTPUT"') < run.index("--replicas=0")
    assert 'kubectl -n "$ns" get pods -l app=sentinel-backend -o name' in run
    assert "backend pods still running after 300 s" in run


def test_job_step_renders_the_manifest() -> None:
    step = _step("eval", "job")
    run = step["run"]
    assert "azure/k8s/jobs/eval-job.yaml" in run
    assert "--cascade=foreground" in run
    assert "2820" in run
    assert "--ignore-not-found" in run
    match = re.search(r"envsubst '([^']*)'", run)
    assert match
    names = set(PLACEHOLDER_RE.findall(match.group(1)))
    assert names == set(PLACEHOLDER_RE.findall(MANIFEST.read_text(encoding="utf-8")))
    assert set(step["env"]) == names
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert 2820 > manifest["spec"]["activeDeadlineSeconds"]


def test_refresh_login_precedes_job() -> None:
    ids = [s["id"] for s in _steps("eval")]
    refresh = _step("eval", "refresh-login")
    assert refresh["uses"].startswith("azure/login@")
    assert refresh["with"] == _step("eval", "login")["with"]
    assert ids.index("refresh-login") == ids.index("job") - 1
    assert f"az account get-access-token --resource {AKS_RESOURCE}" in _step("eval", "job")["run"]


def test_collect_upload_verdict_always() -> None:
    for step_id in ("collect", "upload", "verdict"):
        assert _if(_step("eval", step_id)["if"]).startswith("always() && "), step_id
    upload = _step("eval", "upload")
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["name"] == "eval-report"
    assert upload["with"]["path"].split() == [
        "reports/eval_report.json",
        "reports/eval_report.md",
    ]
    assert upload["with"]["if-no-files-found"] == "error"
    collect = _step("eval", "collect")["run"]
    assert 'python3 scripts/eval_report_from_logs.py "$log" reports' in collect
    # The raw log stays on the runner.
    assert "cat " not in collect
    verdict = _step("eval", "verdict")
    assert verdict["env"]["JOB_RESULT"] == "${{ steps.job.outputs.result }}"
    assert verdict["env"]["REPORT"] == "${{ steps.collect.outputs.report }}"


def test_teardown_paths() -> None:
    clear = _step("teardown-backend", "clear")
    assert "kubectl -n sentinel-dev delete job/sentinel-eval --ignore-not-found" in clear["run"]
    assert "kubelogin convert-kubeconfig -l azurecli" in clear["run"]
    assert '"$state" = "Stopped"' in clear["run"]
    warm = _step("teardown-backend", "restore-warm")
    assert (
        _if(warm["if"])
        == "always() && vars.SENTINEL_KEEP_WARM == 'true' && needs.eval.outputs.paused == 'true'"
    )
    down = _step("teardown-backend", "down")
    assert _if(down["if"]) == "always() && vars.SENTINEL_KEEP_WARM != 'true'"
    assert down["uses"] == "./.github/actions/backend-down"
    assert down["with"] == {
        "resource-group": "${{ vars.AKS_RESOURCE_GROUP }}",
        "cluster-name": "${{ vars.AKS_CLUSTER_NAME }}",
        "keep-warm": "${{ vars.SENTINEL_KEEP_WARM }}",
    }
    assert [s["id"] for s in _steps("teardown-backend")] == [
        "checkout",
        "login",
        "clear",
        "restore-warm",
        "down",
    ]


def test_ops_events_downloads_the_report() -> None:
    report = _step("ops-events", "report")
    assert report["uses"].startswith("actions/download-artifact@")
    assert report["with"] == {"name": "eval-report", "path": "reports"}
    slots = [
        s["id"] for s in _steps("ops-events") if s.get("uses") == "./.github/actions/ops-event"
    ]
    assert tuple(slots) == _emit().EVAL_SLOTS


# ── behaviour: the inputs step ────────────────────────────────────────────────


def _run_inputs(env: dict[str, str], tmp_path: Path) -> tuple[int, str, dict[str, str]]:
    output = tmp_path / "github_output"
    output.write_text("")
    proc = subprocess.run(
        ["bash", "-c", _step("eval", "inputs")["run"]],
        env={
            "PATH": os.environ["PATH"],
            "GITHUB_OUTPUT": str(output),
            "GITHUB_RUN_ID": "7",
            "GITHUB_RUN_ATTEMPT": "1",
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    outputs = dict(line.partition("=")[::2] for line in output.read_text().splitlines())
    return proc.returncode, proc.stdout + proc.stderr, outputs


def test_inputs_step_defaults(tmp_path: Path) -> None:
    rc, out, outputs = _run_inputs({}, tmp_path)
    assert rc == 0, out
    assert outputs == {"dataset": "sentinel-scenario-runs", "run-name": "eval-gha-7-1"}


def test_inputs_step_label(tmp_path: Path) -> None:
    rc, out, outputs = _run_inputs({"RUN_LABEL": "weekly-a", "DATASET": "ds.v2"}, tmp_path)
    assert rc == 0, out
    assert outputs == {"dataset": "ds.v2", "run-name": "weekly-a-gha-7-1"}


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ({"DATASET": "a b"}, "dataset"),
        ({"DATASET": "-lead"}, "dataset"),
        ({"RUN_LABEL": "Bad_Label"}, "run-label"),
        ({"RUN_LABEL": "x" * 41}, "run-label"),
    ],
)
def test_inputs_step_rejects(env: dict[str, str], name: str, tmp_path: Path) -> None:
    rc, out, outputs = _run_inputs(env, tmp_path)
    assert rc == 1
    assert "::error::ci_eval:" in out
    assert f"input {name}" in out
    for value in env.values():
        assert value not in out
    assert outputs == {}


def test_backend_restore_survives_earlier_failures() -> None:
    """A failed login, Job delete or eval Job never leaves the paused backend at 0 replicas."""
    teardown = _jobs()["teardown-backend"]
    assert _if(teardown["if"]).startswith("always() && ")
    assert "eval" in teardown["needs"]
    steps = _steps("teardown-backend")
    restore_ids = [
        s["id"]
        for s in steps
        if s.get("uses") in ("./.github/actions/backend-up", "./.github/actions/backend-down")
    ]
    assert restore_ids == ["restore-warm", "down"]
    for step_id in restore_ids:
        assert _if(_step("teardown-backend", step_id)["if"]).startswith("always() && "), step_id
    # The steps before them may fail without skipping them; none restores on its own.
    before = [s for s in steps if s["id"] not in restore_ids]
    assert [s["id"] for s in before] == ["checkout", "login", "clear"]
    # Idempotent: backend-up scales to exactly 1 after its apply; backend-down ignores what is gone.
    up = (REPO_ROOT / ".github/actions/backend-up/action.yml").read_text(encoding="utf-8")
    assert "scale deployment/sentinel-backend --replicas=1" in up
    down = (REPO_ROOT / ".github/actions/backend-down/action.yml").read_text(encoding="utf-8")
    assert "delete service/sentinel-backend --ignore-not-found" in down
    # No other job of this workflow scales the backend up.
    for job_id, job in _jobs().items():
        if job_id == "teardown-backend":
            continue
        for step in job.get("steps", []):
            assert step.get("uses") != "./.github/actions/backend-down", job_id
            if job_id != "eval":
                assert step.get("uses") != "./.github/actions/backend-up", job_id
