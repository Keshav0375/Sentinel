"""azure/k8s/jobs/eval-job.yaml: the in-cluster eval Job (decision 2026-10-28).

The Job runs the image's eval CLI under the backend's workload identity (SA sentinel-backend, the
WI label, envFrom sentinel-config), so the LLM keys come from Key Vault inside the pod only. Its
pod and container hardening, volumes and resources equal the Deployment's. It sits outside
backend-up's azure/k8s/*.yaml glob and is rendered only by ci_eval.yml. Offline; the kubectl
dry-run needs SENTINEL_TEST_KUBECTL_CONTEXT.
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

REPO = Path(__file__).resolve().parents[2]
K8S_DIR = REPO / "azure" / "k8s"
MANIFEST = K8S_DIR / "jobs" / "eval-job.yaml"
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
KUBECTL_CONTEXT_ENV = "SENTINEL_TEST_KUBECTL_CONTEXT"

SAMPLE: dict[str, str] = {
    "ACR_LOGIN_SERVER": "sentinelacrtest.azurecr.io",
    "IMAGE_TAG": "sha-0730682",
    "EVAL_DATASET": "sentinel-scenario-runs",
    "EVAL_RUN_NAME": "eval-gha-1-1",
}
COMMAND = [
    "python",
    "scripts/run_eval.py",
    "--branches",
    "src/sentinel/catalog/branches.yaml",
    "--dataset",
    "${EVAL_DATASET}",
    "--run-name",
    "${EVAL_RUN_NAME}",
    "--print-report",
]


def _raw() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def _render(text: str, values: dict[str, str]) -> str:
    """Substitute ${N} only for listed names (envsubst explicit-list semantics)."""
    return PLACEHOLDER_RE.sub(lambda m: values.get(m[1], m[0]), text)


def _docs(text: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(text) if d is not None]


def _job() -> dict[str, Any]:
    (doc,) = _docs(_render(_raw(), SAMPLE))
    return doc


def _job_raw() -> dict[str, Any]:
    (doc,) = _docs(_raw())
    return doc


def _deployment() -> dict[str, Any]:
    (doc,) = _docs(_render((K8S_DIR / "deployment.yaml").read_text(encoding="utf-8"), SAMPLE))
    return doc


def _pod(doc: dict[str, Any]) -> dict[str, Any]:
    pod: dict[str, Any] = doc["spec"]["template"]["spec"]
    return pod


def _container(doc: dict[str, Any]) -> dict[str, Any]:
    (container,) = _pod(doc)["containers"]
    return container


def test_location_outside_backend_up_glob() -> None:
    assert MANIFEST.is_file()
    assert MANIFEST not in set(K8S_DIR.glob("*.yaml"))
    assert MANIFEST.parent != K8S_DIR


def test_placeholders_exact() -> None:
    assert set(PLACEHOLDER_RE.findall(_raw())) == set(SAMPLE)
    rendered = _render(_raw(), SAMPLE)
    assert "${" not in rendered
    assert re.search(r"\$(?!\{)", _raw()) is None


def test_one_namespaceless_job() -> None:
    job = _job()
    assert job["apiVersion"] == "batch/v1"
    assert job["kind"] == "Job"
    assert job["metadata"]["name"] == "sentinel-eval"
    assert job["metadata"]["labels"] == {"app": "sentinel-eval"}
    assert "namespace" not in job["metadata"]


def test_job_bounds() -> None:
    spec = _job()["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] == 2700
    assert spec["ttlSecondsAfterFinished"] == 3600
    assert _pod(_job())["restartPolicy"] == "Never"


def test_workload_identity() -> None:
    labels = _job()["spec"]["template"]["metadata"]["labels"]
    assert labels == {"app": "sentinel-eval", "azure.workload.identity/use": "true"}
    assert isinstance(labels["azure.workload.identity/use"], str)
    pod = _pod(_job())
    assert pod["serviceAccountName"] == "sentinel-backend"
    assert pod["automountServiceAccountToken"] is False


def test_matches_the_deployment_pod() -> None:
    job, deployment = _job(), _deployment()
    for key in ("securityContext", "volumes", "serviceAccountName", "automountServiceAccountToken"):
        assert _pod(job)[key] == _pod(deployment)[key], key
    for key in ("securityContext", "resources", "envFrom", "volumeMounts", "imagePullPolicy"):
        assert _container(job)[key] == _container(deployment)[key], key
    assert _container(job)["envFrom"] == [{"configMapRef": {"name": "sentinel-config"}}]
    assert (
        _container(_job_raw())["image"]
        == _container(_docs((K8S_DIR / "deployment.yaml").read_text(encoding="utf-8"))[0])["image"]
    )
    assert _container(job)["image"] == "sentinelacrtest.azurecr.io/sentinel-backend:sha-0730682"


def test_command_runs_the_shipped_cli() -> None:
    container = _container(_job_raw())
    assert container["command"] == COMMAND
    assert container["workingDir"] == "/app"
    assert (REPO / "src/sentinel/catalog/branches.yaml").exists()
    assert (REPO / "scripts/run_eval.py").exists()
    for key in ("ports", "readinessProbe", "livenessProbe", "args"):
        assert key not in container, key
    assert "kind: Secret" not in _raw()
    rendered = _container(_job())["command"]
    assert rendered[5] == "sentinel-scenario-runs"
    assert rendered[7] == "eval-gha-1-1"


def test_selector_isolation() -> None:
    # The NetworkPolicy, the Service and the pause loop select app=sentinel-backend.
    assert _job()["metadata"]["labels"]["app"] != "sentinel-backend"
    assert _job()["spec"]["template"]["metadata"]["labels"]["app"] != "sentinel-backend"


def test_kubectl_client_dry_run(tmp_path: Path) -> None:
    ctx = os.environ.get(KUBECTL_CONTEXT_ENV)
    if not ctx:
        pytest.skip(f"{KUBECTL_CONTEXT_ENV} not set")
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl not on PATH")
    rendered = tmp_path / "eval-job.yaml"
    rendered.write_text(_render(_raw(), SAMPLE), encoding="utf-8")
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            ctx,
            "--request-timeout=20s",
            "-n",
            "sentinel-dev",
            "apply",
            "--dry-run=client",
            "-f",
            str(rendered),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "job.batch/sentinel-eval" in result.stdout
    assert "(dry run)" in result.stdout
