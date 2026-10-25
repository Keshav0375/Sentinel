"""Offline static checks of .github/workflows/ci_backend_scale.yml (backend task 8.4; decisions
2026-10-24), plus its use of the shared resolve-stable-tag action (the lookup itself is tested with
the composite actions).

Nothing here calls Azure.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/ci_backend_scale.yml"

UP_IF = "github.event_name == 'workflow_dispatch' && inputs.direction == 'up'"
DOWN_IF = "github.event_name == 'workflow_dispatch' && inputs.direction == 'down'"
NIGHTLY_IF = "github.event_name == 'schedule'"

RG_EXPR = "${{ vars.AKS_RESOURCE_GROUP }}"
CLUSTER_EXPR = "${{ vars.AKS_CLUSTER_NAME }}"

EXPECTED_VARS = frozenset(
    {
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
        "AKS_RESOURCE_GROUP",
        "AKS_CLUSTER_NAME",
        "ACR_NAME",
        "ACR_LOGIN_SERVER",
        "SENTINEL_API_TENANT_ID",
        "SENTINEL_API_AUDIENCE",
        "KEY_VAULT_URI",
        "PG_HOST",
        "PG_DATABASE",
        "PG_USER_BACKEND",
        "SENTINEL_KEEP_WARM",
    }
)

UP_WITH = {
    "resource-group": "${{ vars.AKS_RESOURCE_GROUP }}",
    "cluster-name": "${{ vars.AKS_CLUSTER_NAME }}",
    "image-tag": "${{ steps.resolve-tag.outputs.image-tag }}",
    "acr-login-server": "${{ vars.ACR_LOGIN_SERVER }}",
    "api-tenant-id": "${{ vars.SENTINEL_API_TENANT_ID }}",
    "api-audience": "${{ vars.SENTINEL_API_AUDIENCE }}",
    "key-vault-url": "${{ vars.KEY_VAULT_URI }}",
    "pg-host": "${{ vars.PG_HOST }}",
    "pg-database": "${{ vars.PG_DATABASE }}",
    "pg-user": "${{ vars.PG_USER_BACKEND }}",
}


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _doc() -> dict[str, Any]:
    return yaml.safe_load(_text())


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) parses the bare key `on` as True.
    return doc["on"] if "on" in doc else doc[True]


def _steps() -> list[dict[str, Any]]:
    return _doc()["jobs"]["scale"]["steps"]


def _step(step_id: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("id") == step_id:
            return step
    pytest.fail(f"no step with id {step_id!r}")


def test_name() -> None:
    assert _doc()["name"] == "[sentinel] backend — scale"


def test_triggers_are_dispatch_and_nightly_cron() -> None:
    on = _on(_doc())
    assert set(on) == {"workflow_dispatch", "schedule"}
    assert on["schedule"] == [{"cron": "0 2 * * *"}]


def test_direction_input_is_up_down_choice() -> None:
    inputs = _on(_doc())["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"direction"}
    direction = inputs["direction"]
    assert direction["type"] == "choice"
    assert direction["options"] == ["up", "down"]
    assert direction["required"] is True
    assert direction["default"] == "down"


def test_concurrency_shared_and_never_cancels() -> None:
    assert _doc()["concurrency"] == {"group": "sentinel-backend", "cancel-in-progress": False}


def test_permissions_minimal() -> None:
    doc = _doc()
    assert doc["permissions"] == {"id-token": "write", "contents": "read"}
    assert "permissions" not in doc["jobs"]["scale"]


def test_single_scale_job_in_sentinel_dev() -> None:
    doc = _doc()
    assert list(doc["jobs"]) == ["scale"]
    job = doc["jobs"]["scale"]
    assert job["environment"] == "sentinel-dev"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 60


def test_checkout_then_azure_login_first() -> None:
    steps = _steps()
    assert steps[0]["uses"] == "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
    assert steps[0]["with"]["persist-credentials"] is False
    assert steps[1]["uses"] == "azure/login@7184910d9eb2b1c5e48f7073824a90609bb9b6d6"
    assert steps[1]["with"] == {
        "client-id": "${{ vars.AZURE_CLIENT_ID }}",
        "tenant-id": "${{ vars.AZURE_TENANT_ID }}",
        "subscription-id": "${{ vars.AZURE_SUBSCRIPTION_ID }}",
    }
    for index, step in enumerate(steps):
        if str(step.get("uses", "")).startswith("./.github/actions/"):
            assert index > 1


def test_up_uses_backend_up_with_vars() -> None:
    up = _step("up")
    assert up["uses"] == "./.github/actions/backend-up"
    assert up["if"] == UP_IF
    assert up["with"] == UP_WITH


def test_up_path_steps_only_on_manual_up() -> None:
    for step_id in ("resolve-tag", "up", "summary"):
        assert _step(step_id)["if"] == UP_IF


def test_backend_url_output_and_summary() -> None:
    job = _doc()["jobs"]["scale"]
    assert job["outputs"] == {"backend-url": "${{ steps.up.outputs.backend-url }}"}
    summary = _step("summary")
    assert summary["env"]["BACKEND_URL"] == "${{ steps.up.outputs.backend-url }}"
    assert "$GITHUB_STEP_SUMMARY" in summary["run"]


def test_manual_down_honours_keep_warm() -> None:
    down = _step("down-manual")
    assert down["uses"] == "./.github/actions/backend-down"
    assert down["if"] == DOWN_IF
    assert down["with"] == {
        "resource-group": RG_EXPR,
        "cluster-name": CLUSTER_EXPR,
        "keep-warm": "${{ vars.SENTINEL_KEEP_WARM }}",
    }


def test_nightly_down_never_keeps_warm() -> None:
    down = _step("down-nightly")
    assert down["uses"] == "./.github/actions/backend-down"
    assert down["if"] == NIGHTLY_IF
    assert isinstance(down["with"]["keep-warm"], str)
    assert down["with"]["keep-warm"] == "false"
    assert down["with"]["resource-group"] == RG_EXPR
    assert down["with"]["cluster-name"] == CLUSTER_EXPR


def test_schedule_never_scales_up() -> None:
    steps = _steps()
    up_steps = [s for s in steps if s.get("uses") == "./.github/actions/backend-up"]
    assert len(up_steps) == 1
    for step in steps:
        if step.get("uses") == "./.github/actions/backend-up" or step.get("id") == "resolve-tag":
            assert "github.event_name == 'workflow_dispatch'" in step["if"]


def test_cluster_never_stopped() -> None:
    text = _text()
    assert re.search(r"aks\s+stop", text) is None
    assert "az aks stop" not in text


def test_run_steps_take_inputs_via_env_only() -> None:
    for step in _steps():
        if "run" in step:
            assert "${{" not in step["run"]


def test_vars_are_the_contract_names() -> None:
    text = _text()
    assert set(re.findall(r"vars\.([A-Za-z0-9_]+)", text)) == EXPECTED_VARS
    assert "secrets." not in text


def test_resolve_tag_uses_the_shared_action() -> None:
    step = _step("resolve-tag")
    assert step["uses"] == "./.github/actions/resolve-stable-tag"
    assert step["with"] == {"acr-name": "${{ vars.ACR_NAME }}"}
    assert "run" not in step
    assert step["if"] == UP_IF
