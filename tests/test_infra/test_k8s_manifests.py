"""Offline contract checks for azure/k8s/ (backend task 7.2; decisions 2026-10-24)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from sentinel.api.health import READY_PING_TIMEOUT_S
from sentinel.config import Settings

K8S_DIR = Path(__file__).resolve().parents[2] / "azure" / "k8s"

MANIFESTS = {
    "configmap.yaml": ("v1", "ConfigMap", "sentinel-config"),
    "deployment.yaml": ("apps/v1", "Deployment", "sentinel-backend"),
    "networkpolicy.yaml": ("networking.k8s.io/v1", "NetworkPolicy", "sentinel-backend"),
    "service.yaml": ("v1", "Service", "sentinel-backend"),
}

APP_LABELS = {"app": "sentinel-backend"}

PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

SAMPLE_VALUES: dict[str, str] = {
    "ACR_LOGIN_SERVER": "sentinelacrtest.azurecr.io",
    "IMAGE_TAG": "sha-0730682",
    "SENTINEL_API_TENANT_ID": "00000000-0000-0000-0000-000000000001",
    "SENTINEL_API_AUDIENCE": (
        "00000000-0000-0000-0000-000000000002,"
        "api://00000000-0000-0000-0000-000000000001/sentinel-backend-sentinel-dev"
    ),
    "KEY_VAULT_URL": "https://kv-sentinel-test.vault.azure.net/",
    "PGHOST": "pg-sentinel-test.postgres.database.azure.com",
    "PGDATABASE": "sentinel_test",
    "PGUSER": "id-sentinel-backend-test",
    "REDIS_HOST": "redis-sentinel-test.canadacentral.redis.azure.net",
    "REDIS_PORT": "10000",
}

KUBECTL_CONTEXT_ENV = "SENTINEL_TEST_KUBECTL_CONTEXT"


def _placeholder_names() -> list[str]:
    """Names listed in placeholders.txt (blank and # lines dropped)."""
    lines = (K8S_DIR / "placeholders.txt").read_text(encoding="utf-8").splitlines()
    stripped = (line.strip() for line in lines)
    return [line for line in stripped if line and not line.startswith("#")]


def _raw(name: str) -> str:
    """Unrendered text of one manifest."""
    return (K8S_DIR / name).read_text(encoding="utf-8")


def _render(text: str, values: dict[str, str]) -> str:
    """Substitute ${N} only for listed names (envsubst explicit-list semantics)."""
    names = set(_placeholder_names())
    return PLACEHOLDER_RE.sub(lambda m: values[m[1]] if m[1] in names else m[0], text)


def _doc(name: str) -> dict[str, Any]:
    """The single rendered YAML document of one manifest."""
    docs = [d for d in yaml.safe_load_all(_render(_raw(name), SAMPLE_VALUES)) if d is not None]
    assert len(docs) == 1
    return docs[0]


def _container() -> dict[str, Any]:
    """The Deployment's only container."""
    containers = _doc("deployment.yaml")["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def test_k8s_dir_holds_exactly_the_four_manifests() -> None:
    found = {p.name for p in K8S_DIR.iterdir() if p.suffix in {".yaml", ".yml", ".json"}}
    assert found == set(MANIFESTS)
    assert not (K8S_DIR / "serviceaccount.yaml").exists()
    for name in MANIFESTS:
        assert "kind: Secret" not in _raw(name)


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_each_manifest_is_one_namespaceless_resource(name: str) -> None:
    api_version, kind, resource_name = MANIFESTS[name]
    doc = _doc(name)
    assert doc["apiVersion"] == api_version
    assert doc["kind"] == kind
    assert doc["metadata"]["name"] == resource_name
    assert doc["metadata"]["labels"] == APP_LABELS
    assert "namespace" not in doc["metadata"]


def test_deployment_shape() -> None:
    spec = _doc("deployment.yaml")["spec"]
    pod = spec["template"]["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"] == {"type": "Recreate"}
    assert spec["selector"]["matchLabels"] == APP_LABELS
    labels = spec["template"]["metadata"]["labels"]
    assert labels == {"app": "sentinel-backend", "azure.workload.identity/use": "true"}
    assert isinstance(labels["azure.workload.identity/use"], str)
    assert pod["serviceAccountName"] == "sentinel-backend"
    assert pod["terminationGracePeriodSeconds"] == 45
    container = _container()
    assert container["name"] == "sentinel-backend"
    assert container["ports"] == [{"name": "http", "containerPort": 8000, "protocol": "TCP"}]
    assert pod["automountServiceAccountToken"] is False
    assert pod["volumes"] == [{"name": "reports", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}}]
    assert container["volumeMounts"] == [
        {"name": "reports", "mountPath": "/app/reports"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    assert "imagePullSecrets" not in pod


def test_deployment_image_is_templated() -> None:
    assert 'image: "${ACR_LOGIN_SERVER}/sentinel-backend:${IMAGE_TAG}"' in _raw("deployment.yaml")
    image = _container()["image"]
    assert image == "sentinelacrtest.azurecr.io/sentinel-backend:sha-0730682"
    assert re.match(r"^[^/]+/sentinel-backend:sha-[0-9a-f]{7}$", image)


def test_deployment_probes() -> None:
    container = _container()
    ready = container["readinessProbe"]
    live = container["livenessProbe"]
    assert ready["httpGet"] == {"path": "/ready", "port": 8000}
    assert ready["initialDelaySeconds"] == 10
    assert ready["timeoutSeconds"] > READY_PING_TIMEOUT_S
    assert live["httpGet"] == {"path": "/health", "port": 8000}
    assert live["initialDelaySeconds"] == 15


def test_deployment_resources() -> None:
    assert _container()["resources"] == {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "1", "memory": "2Gi"},
    }


def test_deployment_config_is_configmap_only() -> None:
    container = _container()
    assert container["envFrom"] == [{"configMapRef": {"name": "sentinel-config"}}]
    assert "env" not in container
    for name in MANIFESTS:
        raw = _raw(name)
        assert "secretRef" not in raw
        assert "secretKeyRef" not in raw


def test_deployment_security_context() -> None:
    pod = _doc("deployment.yaml")["spec"]["template"]["spec"]
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert _container()["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }


def test_configmap_data() -> None:
    dynamic = [
        "SENTINEL_API_TENANT_ID",
        "SENTINEL_API_AUDIENCE",
        "KEY_VAULT_URL",
        "PGHOST",
        "PGDATABASE",
        "PGUSER",
        "REDIS_HOST",
        "REDIS_PORT",
    ]
    data = _doc("configmap.yaml")["data"]
    assert set(data) == {*dynamic, "DD_SITE", "SENTINEL_PRIMARY_PROVIDER"}
    assert data["DD_SITE"] == "us5.datadoghq.com"
    assert data["SENTINEL_PRIMARY_PROVIDER"] == "anthropic"
    assert all(isinstance(v, str) for v in data.values())
    raw_data = yaml.safe_load(_raw("configmap.yaml"))["data"]
    for key in dynamic:
        assert raw_data[key] == "${" + key + "}"
    for absent in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "SENTINEL_DEV_OPEN_DASHBOARD",
        "DATABASE_URL",
    ):
        assert absent not in data


def test_configmap_keys_are_settings_fields() -> None:
    for key in _doc("configmap.yaml")["data"]:
        assert key.lower() in Settings.model_fields


def test_service_shape() -> None:
    spec = _doc("service.yaml")["spec"]
    assert spec["type"] == "LoadBalancer"
    assert spec["selector"] == APP_LABELS
    assert spec["ports"] == [{"name": "http", "protocol": "TCP", "port": 80, "targetPort": 8000}]


def test_networkpolicy_shape() -> None:
    spec = _doc("networkpolicy.yaml")["spec"]
    assert spec["podSelector"] == {"matchLabels": APP_LABELS}
    assert spec["policyTypes"] == ["Ingress"]
    assert spec["ingress"] == [{"ports": [{"protocol": "TCP", "port": 8000}]}]
    assert "egress" not in spec


def test_placeholder_list_matches_manifests() -> None:
    names = _placeholder_names()
    assert len(names) == len(set(names))
    for n in names:
        assert re.match(r"^[A-Z][A-Z0-9_]*$", n)
    raw = "\n".join(_raw(n) for n in MANIFESTS)
    assert set(PLACEHOLDER_RE.findall(raw)) == set(names) == set(SAMPLE_VALUES)
    assert re.search(r"\$(?!\{)", raw) is None


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_python_render_leaves_no_placeholder(name: str) -> None:
    assert "$" not in _render(_raw(name), SAMPLE_VALUES)


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_envsubst_render_matches(name: str) -> None:
    if shutil.which("envsubst") is None:
        pytest.skip("envsubst not on PATH")
    fmt = " ".join("${" + n + "}" for n in _placeholder_names())
    raw = _raw(name)
    result = subprocess.run(
        ["envsubst", fmt],
        input=raw,
        env={"PATH": os.environ["PATH"], **SAMPLE_VALUES},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == _render(raw, SAMPLE_VALUES)
    assert "$" not in result.stdout


def test_kubectl_client_dry_run(tmp_path: Path) -> None:
    ctx = os.environ.get(KUBECTL_CONTEXT_ENV)
    if not ctx:
        pytest.skip(f"{KUBECTL_CONTEXT_ENV} not set")
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl not on PATH")
    for name in MANIFESTS:
        (tmp_path / name).write_text(_render(_raw(name), SAMPLE_VALUES), encoding="utf-8")
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
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    for expected in (
        "configmap/sentinel-config",
        "deployment.apps/sentinel-backend",
        "service/sentinel-backend",
        "networkpolicy.networking.k8s.io/sentinel-backend",
    ):
        assert expected in result.stdout
    assert "(dry run)" in result.stdout
