"""Offline contract checks for .github/actions (backend task 7.3; decisions 2026-10-24).

The scripts run for real under the local bash (3.2 on macOS, 5.x on the runner) with stubbed az,
kubectl, kubelogin, psql and curl. Nothing here calls Azure, a cluster or Teams, and kubectl's
context is never touched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
K8S_DIR = REPO_ROOT / "azure" / "k8s"

ACTIONS = (
    "backend-down",
    "backend-up",
    "get-backend-token",
    "get-db-token",
    "get-kv-secrets",
    "notify-teams",
    "psql-exec",
)

KV_OUTPUTS = {
    "datadog-api-key",
    "datadog-app-key",
    "langfuse-public-key",
    "langfuse-secret-key",
    "langfuse-base-url",
    "teams-webhook-url",
}

# name -> (required, default); default None means the input declares no default.
EXPECTED_INPUTS: dict[str, dict[str, tuple[bool, str | None]]] = {
    "backend-up": {
        "namespace": (False, "sentinel-dev"),
        "resource-group": (True, None),
        "cluster-name": (True, None),
        "image-tag": (True, None),
        "acr-login-server": (True, None),
        "api-tenant-id": (True, None),
        "api-audience": (True, None),
        "key-vault-url": (True, None),
        "pg-host": (True, None),
        "pg-database": (True, None),
        "pg-user": (True, None),
    },
    "backend-down": {
        "namespace": (False, "sentinel-dev"),
        "resource-group": (True, None),
        "cluster-name": (True, None),
        "keep-warm": (False, "false"),
    },
    "get-db-token": {},
    "get-backend-token": {
        "audience": (True, None),
        "tenant": (False, ""),
    },
    "get-kv-secrets": {
        "vault-name": (True, None),
        "names": (True, None),
    },
    "notify-teams": {
        "title": (True, None),
        "body": (True, None),
        "webhook-url": (True, None),
        "severity": (False, "info"),
    },
    "psql-exec": {
        "sql": (True, None),
        "pg-token": (True, None),
        "pg-host": (True, None),
        "pg-database": (True, None),
        "pg-user": (True, None),
        "vars": (False, ""),
    },
}

EXPECTED_OUTPUTS: dict[str, set[str]] = {
    "backend-up": {"backend-url"},
    "backend-down": set(),
    "get-db-token": {"pg-token"},
    "get-backend-token": {"bearer"},
    "get-kv-secrets": KV_OUTPUTS,
    "notify-teams": set(),
    "psql-exec": {"result"},
}

SECRET_OUTPUTS: dict[str, set[str]] = {
    "get-kv-secrets": KV_OUTPUTS,
    "get-db-token": {"pg-token"},
    "get-backend-token": {"bearer"},
}

# Inputs whose value is itself a secret: the consuming script must mask them.
MASKED_INPUT_ENV = {"psql-exec": "PG_TOKEN", "notify-teams": "WEBHOOK_URL"}

# placeholders.txt name -> backend-up input
PLACEHOLDER_INPUTS = {
    "ACR_LOGIN_SERVER": "acr-login-server",
    "IMAGE_TAG": "image-tag",
    "SENTINEL_API_TENANT_ID": "api-tenant-id",
    "SENTINEL_API_AUDIENCE": "api-audience",
    "KEY_VAULT_URL": "key-vault-url",
    "PGHOST": "pg-host",
    "PGDATABASE": "pg-database",
    "PGUSER": "pg-user",
}

INPUT_EXPR = re.compile(r"^\$\{\{ inputs\.([a-z-]+) \}\}$")
OUTPUT_EXPR = re.compile(r"^\$\{\{ steps\.([a-z-]+)\.outputs\.[a-z-]+ \}\}$")

# The Sentinel-deployment .yamllint.yaml rules, inline (no new repo file).
YAMLLINT_CONFIG = """\
extends: default
rules:
  truthy:
    check-keys: false
  line-length:
    max: 120
  document-start: disable
"""

SAMPLE_UP_INPUTS: dict[str, str] = {
    "resource-group": "rg-test",
    "cluster-name": "aks-test",
    "acr-login-server": "sentinelacrtest.azurecr.io",
    "image-tag": "sha-0730682",
    "api-tenant-id": "00000000-0000-0000-0000-000000000001",
    "api-audience": (
        "00000000-0000-0000-0000-000000000002,"
        "api://00000000-0000-0000-0000-000000000001/sentinel-backend-sentinel-dev"
    ),
    "key-vault-url": "https://kv-sentinel-test.vault.azure.net/",
    "pg-host": "pg-sentinel-test.postgres.database.azure.com",
    "pg-database": "sentinel_test",
    "pg-user": "id-sentinel-backend-test",
}

MANIFEST_FILES = {"configmap.yaml", "deployment.yaml", "networkpolicy.yaml", "service.yaml"}

# Stub bodies (run after the stub has logged "<name> <args>" to $STUB_LOG).
AZ_ECHO_POWER = 'case "$1 $2" in "aks show") echo "$STUB_POWER";; esac; exit 0'
KUBECTL_BODY = r"""
case "$*" in
  *" apply "*)
    d=""; prev=""
    for a in "$@"; do
      if [ "$prev" = "-f" ]; then d="$a"; fi
      prev="$a"
    done
    cp -R "$d" "$STUB_LOG.rendered"
    ;;
  *" get svc "*) printf '10.0.0.7' ;;
esac
exit 0
"""


def _action(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((ACTIONS_DIR / name / "action.yml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _steps(name: str) -> list[dict[str, Any]]:
    return list(_action(name)["runs"]["steps"])


def _step(name: str, step_id: str) -> dict[str, Any]:
    return next(s for s in _steps(name) if s.get("id") == step_id)


def _script(step: dict[str, Any]) -> str:
    return str(step["run"])


@dataclass
class StepRun:
    returncode: int
    stdout: str
    outputs: dict[str, str]
    log: list[str]


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "{name} $*" >> "$STUB_LOG"\n{body}\n')
    path.chmod(0o755)


def _run_step(
    action: str,
    step_id: str,
    inputs: dict[str, str],
    tmp_path: Path,
    stubs: dict[str, str],
    extra_env: dict[str, str] | None = None,
) -> StepRun:
    declared: dict[str, Any] = _action(action).get("inputs") or {}
    step = _step(action, step_id)
    resolved: dict[str, str] = {}
    for key, value in (step.get("env") or {}).items():
        match = INPUT_EXPR.match(str(value))
        assert match, f"{action}/{step_id}: env {key} is not a plain input expression"
        name = match.group(1)
        if name in inputs:
            resolved[key] = inputs[name]
        else:
            resolved[key] = str(declared[name].get("default", ""))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for stub_name, body in stubs.items():
        _stub(bin_dir, stub_name, body)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/bash\nexit 0\n")
    sleep.chmod(0o755)

    runner_temp = tmp_path / "runner"
    runner_temp.mkdir(exist_ok=True)
    github_output = tmp_path / f"output-{action}-{step_id}"
    github_output.unlink(missing_ok=True)
    stub_log = tmp_path / "stub.log"
    script = tmp_path / f"{action}-{step_id}.sh"
    script.write_text(_script(step))

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GITHUB_OUTPUT": str(github_output),
        "GITHUB_PATH": str(tmp_path / "github-path"),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_WORKSPACE": str(REPO_ROOT),
        "STUB_LOG": str(stub_log),
        **resolved,
        **(extra_env or {}),
    }
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    outputs: dict[str, str] = {}
    if github_output.exists():
        for line in github_output.read_text().splitlines():
            key, _, value = line.partition("=")
            outputs[key] = value
    log = stub_log.read_text().splitlines() if stub_log.exists() else []
    return StepRun(proc.returncode, proc.stdout + proc.stderr, outputs, log)


def _need(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not on PATH")


# --- static contract ---


def test_actions_dir_holds_the_seven() -> None:
    found = {p.parent.name for p in ACTIONS_DIR.glob("*/action.yml")}
    assert found == set(ACTIONS)
    assert (ACTIONS_DIR / "README.md").is_file()


@pytest.mark.parametrize("action", ACTIONS)
def test_action_io_names(action: str) -> None:
    doc = _action(action)
    assert doc["runs"]["using"] == "composite"

    inputs: dict[str, Any] = doc.get("inputs") or {}
    expected = EXPECTED_INPUTS[action]
    assert set(inputs) == set(expected)
    for name, (required, default) in expected.items():
        assert bool(inputs[name].get("required", False)) is required, name
        if default is None:
            assert "default" not in inputs[name], name
        else:
            assert str(inputs[name]["default"]) == default, name

    outputs: dict[str, Any] = doc.get("outputs") or {}
    assert set(outputs) == EXPECTED_OUTPUTS[action]
    step_ids = {s["id"] for s in _steps(action)}
    for name, spec in outputs.items():
        match = OUTPUT_EXPR.match(spec["value"])
        assert match, name
        assert match.group(1) in step_ids, name


@pytest.mark.parametrize("action", ACTIONS)
def test_steps_are_hardened(action: str) -> None:
    declared = set(_action(action).get("inputs") or {})
    seen: set[str] = set()
    for step in _steps(action):
        assert set(step) <= {"id", "name", "if", "shell", "env", "run"}, step.get("id")
        assert step["shell"] == "bash"
        assert step.get("id"), "every step needs an id"
        assert step["id"] not in seen
        seen.add(step["id"])
        script = _script(step)
        code = [ln for ln in script.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        assert code[0] == "set -euo pipefail"
        assert "${{" not in script
        for key, value in (step.get("env") or {}).items():
            match = INPUT_EXPR.match(str(value))
            assert match, f"{step['id']}: env {key}"
            assert match.group(1) in declared, f"{step['id']}: env {key}"


@pytest.mark.parametrize("action", ACTIONS)
def test_no_inputs_expression_outside_env_and_if(action: str) -> None:
    env_line = re.compile(r"^\s+[A-Z_]+: \$\{\{ inputs\.")
    if_line = re.compile(r"^\s+(- )?if:")
    text = (ACTIONS_DIR / action / "action.yml").read_text()
    for line in text.splitlines():
        if "${{ inputs." in line:
            assert env_line.match(line) or if_line.match(line), line


def test_secret_outputs_are_masked() -> None:
    for action, names in SECRET_OUTPUTS.items():
        outputs = _action(action)["outputs"]
        for name in names:
            step_id = OUTPUT_EXPR.match(outputs[name]["value"]).group(1)  # type: ignore[union-attr]
            script = _script(_step(action, step_id))
            assert "::add-mask::" in script, (action, name)
            assert script.index("::add-mask::") < script.index("GITHUB_OUTPUT"), (action, name)
    for action, var in MASKED_INPUT_ENV.items():
        scripts = [_script(s) for s in _steps(action)]
        assert any(f'echo "::add-mask::${var}"' in s for s in scripts), action


def test_cluster_is_never_stopped() -> None:
    for action in ACTIONS:
        text = (ACTIONS_DIR / action / "action.yml").read_text()
        assert re.search(r"aks\s+stop", text) is None, action
    assert "az aks start" in (ACTIONS_DIR / "backend-up" / "action.yml").read_text()


def test_psql_exec_has_no_password_input() -> None:
    inputs = _action("psql-exec")["inputs"]
    assert "pg-token" in inputs
    for action in ACTIONS:
        for name in _action(action).get("inputs") or {}:
            assert "pass" not in name.lower(), (action, name)
        for step in _steps(action):
            for key in step.get("env") or {}:
                assert "PASSWORD" not in key, (action, key)


def test_backend_up_placeholders_match() -> None:
    names = {
        line.strip()
        for line in (K8S_DIR / "placeholders.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    env: dict[str, str] = _step("backend-up", "apply")["env"]
    assert set(env) - {"NAMESPACE"} == names == set(PLACEHOLDER_INPUTS)
    for key, value in env.items():
        if key != "NAMESPACE":
            assert value == f"${{{{ inputs.{PLACEHOLDER_INPUTS[key]} }}}}"


@pytest.mark.parametrize("action", ["backend-up", "backend-down"])
def test_kubectl_calls_are_namespaced(action: str) -> None:
    for step in _steps(action):
        for line in _script(step).splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in re.finditer(r"\bkubectl\s", line):
                assert line[match.end() :].startswith('-n "$NAMESPACE" '), line


def test_backend_down_honours_keep_warm() -> None:
    keep = _step("backend-down", "keep-warm")
    assert keep["if"] == "inputs.keep-warm == 'true'"
    assert "kubectl" not in _script(keep)
    assert "az " not in _script(keep)
    for step in _steps("backend-down"):
        if step["id"] != "keep-warm":
            assert "inputs.keep-warm != 'true'" in step["if"], step["id"]
    teardown = _script(_step("backend-down", "teardown"))
    assert 'delete -f "$k8s/service.yaml" --ignore-not-found' in teardown
    assert "--replicas=0" in teardown


def test_readme_documents_pinning_and_trust() -> None:
    readme = (ACTIONS_DIR / "README.md").read_text()
    assert re.search(r"uses: Keshav0375/Sentinel/\.github/actions/[a-z-]+@[0-9a-f]{40}", readme)
    assert re.search(r"uses: \S+@main", readme) is None
    assert "trusted ref" in readme
    assert "identity tenant" in readme
    for action in ACTIONS:
        assert action in readme, action


def test_backend_never_invokes_actions() -> None:
    for path in (REPO_ROOT / "src").rglob("*"):
        if path.is_file():
            assert ".github/actions" not in path.read_text(errors="ignore"), path


# --- lint ---


def test_actionlint_probe_workflow(tmp_path: Path) -> None:
    _need("actionlint")
    shutil.copytree(ACTIONS_DIR, tmp_path / ".github" / "actions")
    (tmp_path / ".git").mkdir()
    jobs: dict[str, Any] = {}
    for action in ACTIONS:
        doc = _action(action)
        step: dict[str, Any] = {"id": "a", "uses": f"./.github/actions/{action}"}
        if doc.get("inputs"):
            step["with"] = {name: "x" for name in doc["inputs"]}
        steps = [step]
        outputs = sorted(doc.get("outputs") or {})
        if outputs:
            steps.append(
                {
                    "run": 'echo "' + " ".join(f"$O{i}" for i in range(len(outputs))) + '"',
                    "env": {
                        f"O{i}": f"${{{{ steps.a.outputs.{name} }}}}"
                        for i, name in enumerate(outputs)
                    },
                }
            )
        jobs[action] = {"runs-on": "ubuntu-latest", "steps": steps}
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir()
    (workflows / "probe.yml").write_text(
        yaml.safe_dump({"on": "workflow_dispatch", "jobs": jobs}, sort_keys=False)
    )
    proc = subprocess.run(
        ["actionlint", "-no-color"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_shellcheck_run_blocks(tmp_path: Path) -> None:
    _need("shellcheck")
    files: list[str] = []
    for action in ACTIONS:
        for step in _steps(action):
            path = tmp_path / f"{action}__{step['id']}.sh"
            path.write_text("#!/usr/bin/env bash\n" + _script(step))
            files.append(path.name)
    proc = subprocess.run(
        ["shellcheck", "--shell=bash", "--severity=style", *files],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_yamllint_actions() -> None:
    _need("yamllint")
    proc = subprocess.run(
        ["yamllint", "-s", "-d", YAMLLINT_CONFIG, ".github/actions/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- dry runs (stubs only) ---

KV_AZ = r"""
while [ $# -gt 0 ]; do
  if [ "$1" = "--name" ]; then echo "value-of-$2"; exit 0; fi
  shift
done
exit 1
"""


def test_get_kv_secrets_fetches_masks_outputs(tmp_path: Path) -> None:
    run = _run_step(
        "get-kv-secrets",
        "fetch",
        {"vault-name": "kv-test", "names": "datadog-api-key, teams-webhook-url"},
        tmp_path,
        {"az": KV_AZ},
    )
    assert run.returncode == 0, run.stdout
    assert run.outputs == {
        "datadog-api-key": "value-of-datadog-api-key",
        "teams-webhook-url": "value-of-teams-webhook-url",
    }
    assert "::add-mask::value-of-datadog-api-key" in run.stdout
    assert "::add-mask::value-of-teams-webhook-url" in run.stdout
    assert (
        "az keyvault secret show --vault-name kv-test --name datadog-api-key --query value -o tsv"
        in run.log
    )


def test_get_kv_secrets_rejects_undeclared_name(tmp_path: Path) -> None:
    run = _run_step(
        "get-kv-secrets",
        "fetch",
        {"vault-name": "kv-test", "names": "teams-webhook-url github-pat"},
        tmp_path,
        {"az": KV_AZ},
    )
    assert run.returncode != 0
    assert "::error::" in run.stdout
    assert run.log == []
    assert run.outputs == {}


def test_get_kv_secrets_rejects_empty_secret(tmp_path: Path) -> None:
    run = _run_step(
        "get-kv-secrets",
        "fetch",
        {"vault-name": "kv-test", "names": "datadog-api-key"},
        tmp_path,
        {"az": "exit 0"},
    )
    assert run.returncode != 0
    assert run.outputs == {}


def test_get_db_token_masks_and_outputs(tmp_path: Path) -> None:
    run = _run_step("get-db-token", "token", {}, tmp_path, {"az": "echo pg-tok-123"})
    assert run.returncode == 0, run.stdout
    assert run.outputs == {"pg-token": "pg-tok-123"}
    assert "::add-mask::pg-tok-123" in run.stdout
    assert (
        "account get-access-token --resource https://ossrdbms-aad.database.windows.net"
        in run.log[0]
    )


@pytest.mark.parametrize("tenant", ["", "tid-1"])
def test_get_backend_token_scope_and_tenant(tmp_path: Path, tenant: str) -> None:
    audience = "00000000-0000-0000-0000-000000000002"
    run = _run_step(
        "get-backend-token",
        "token",
        {"audience": audience, "tenant": tenant},
        tmp_path,
        {"az": "echo bearer-xyz"},
    )
    assert run.returncode == 0, run.stdout
    assert f"--scope {audience}/.default" in run.log[0]
    assert ("--tenant tid-1" in run.log[0]) is bool(tenant)
    assert run.outputs == {"bearer": "bearer-xyz"}
    assert "::add-mask::bearer-xyz" in run.stdout


def test_get_backend_token_rejects_comma_list(tmp_path: Path) -> None:
    run = _run_step(
        "get-backend-token", "token", {"audience": "a,b"}, tmp_path, {"az": "echo bearer-xyz"}
    )
    assert run.returncode != 0
    assert run.log == []


PSQL_BODY = r"""
printf '%s\n' "env PGHOST=$PGHOST PGDATABASE=$PGDATABASE PGUSER=$PGUSER" >> "$STUB_LOG"
printf '%s\n' "env PGPASSWORD=$PGPASSWORD PGSSLMODE=$PGSSLMODE" >> "$STUB_LOG"
while [ $# -gt 0 ]; do
  if [ "$1" = "-f" ]; then cp "$2" "$STUB_LOG.sql"; fi
  shift
done
printf '[{"id": 42}]\n'
"""

PSQL_CONN = {"pg-token": "tok-1", "pg-host": "h", "pg-database": "d", "pg-user": "u"}


def test_psql_exec_values_travel_as_v_vars(tmp_path: Path) -> None:
    run = _run_step(
        "psql-exec",
        "query",
        {
            "sql": "UPDATE incidents SET status = :'status' WHERE id = :'id' RETURNING id; \n",
            "vars": "status=rolled_back\nid=42\n",
            **PSQL_CONN,
        },
        tmp_path,
        {"psql": PSQL_BODY},
    )
    assert run.returncode == 0, run.stdout
    assert run.outputs == {"result": '[{"id": 42}]'}
    joined = "\n".join(run.log)
    for expected in (
        "-v ON_ERROR_STOP=1",
        "-v status=rolled_back",
        "-v id=42",
        "PGPASSWORD=tok-1",
        "PGSSLMODE=require",
        "PGHOST=h",
    ):
        assert expected in joined, expected
    sql = Path(f"{tmp_path / 'stub.log'}.sql").read_text()
    assert sql.startswith("WITH r AS (\n")
    assert "RETURNING id\n)" in sql
    assert "jsonb_agg" in sql
    assert "rolled_back" not in sql
    assert "42" not in sql
    assert "::add-mask::tok-1" in run.stdout


def test_psql_exec_without_vars(tmp_path: Path) -> None:
    run = _run_step(
        "psql-exec",
        "query",
        {"sql": "SELECT 1 AS id", "vars": "", **PSQL_CONN},
        tmp_path,
        {"psql": PSQL_BODY},
    )
    assert run.returncode == 0, run.stdout
    assert "-v status" not in "\n".join(run.log)


@pytest.mark.parametrize("bad", ["1bad=x", "novalue"])
def test_psql_exec_rejects_bad_vars_line(tmp_path: Path, bad: str) -> None:
    run = _run_step(
        "psql-exec",
        "query",
        {"sql": "SELECT 1 AS id", "vars": bad, **PSQL_CONN},
        tmp_path,
        {"psql": PSQL_BODY},
    )
    assert run.returncode != 0
    assert not any(line.startswith("psql ") for line in run.log)
    assert bad not in run.stdout


CURL_BODY = r"""
while [ $# -gt 0 ]; do
  if [ "$1" = "--data-binary" ]; then p="$2"; cp "${p#@}" "$STUB_LOG.json"; fi
  shift
done
exit 0
"""


def test_notify_teams_posts_adaptive_card(tmp_path: Path) -> None:
    _need("jq")
    run = _run_step(
        "notify-teams",
        "post",
        {
            "title": "Rollback PR opened",
            "body": "line one",
            "severity": "warning",
            "webhook-url": "https://example.invalid/hook",
        },
        tmp_path,
        {"curl": CURL_BODY},
    )
    assert run.returncode == 0, run.stdout
    payload = json.loads(Path(f"{tmp_path / 'stub.log'}.json").read_text())
    assert payload["type"] == "message"
    attachment = payload["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    body = attachment["content"]["body"]
    assert [b["text"] for b in body] == ["Rollback PR opened", "Severity: warning", "line one"]
    assert body[0]["color"] == "Warning"
    assert run.log[0].endswith(" https://example.invalid/hook")
    assert "::add-mask::https://example.invalid/hook" in run.stdout


def test_notify_teams_rejects_unknown_severity(tmp_path: Path) -> None:
    run = _run_step(
        "notify-teams",
        "post",
        {"title": "t", "body": "b", "severity": "high", "webhook-url": "https://example.invalid/h"},
        tmp_path,
        {"curl": CURL_BODY},
    )
    assert run.returncode != 0
    assert not any(line.startswith("curl ") for line in run.log)


def _up_stubs() -> dict[str, str]:
    return {
        "az": AZ_ECHO_POWER,
        "kubelogin": "exit 0",
        "kubectl": KUBECTL_BODY,
        "curl": "exit 0",
    }


@pytest.mark.parametrize("power", ["Stopped", "Running"])
def test_backend_up_dry_run(tmp_path: Path, power: str) -> None:
    _need("envsubst")
    runs = {
        step_id: _run_step(
            "backend-up",
            step_id,
            SAMPLE_UP_INPUTS,
            tmp_path,
            _up_stubs(),
            {"STUB_POWER": power},
        )
        for step_id in ("cluster", "credentials", "apply", "url")
    }
    for step_id, run in runs.items():
        assert run.returncode == 0, (step_id, run.stdout)
    assert runs["url"].outputs == {"backend-url": "http://10.0.0.7"}

    log = runs["url"].log
    joined = "\n".join(log)
    assert ("az aks start --resource-group rg-test --name aks-test" in joined) is (
        power == "Stopped"
    )
    assert "kubelogin convert-kubeconfig -l azurecli" in joined
    kubectl = [line for line in log if line.startswith("kubectl ")]
    assert kubectl
    assert all(line.startswith("kubectl -n sentinel-dev ") for line in kubectl)
    apply_at = next(i for i, line in enumerate(kubectl) if " apply " in line)
    rollout_at = next(i for i, line in enumerate(kubectl) if " rollout " in line)
    assert apply_at < rollout_at
    assert "--timeout=600s" in kubectl[rollout_at]

    rendered = Path(f"{tmp_path / 'stub.log'}.rendered")
    assert {p.name for p in rendered.iterdir()} == MANIFEST_FILES
    for path in rendered.iterdir():
        assert "${" not in path.read_text(), path.name
    assert (
        "sentinelacrtest.azurecr.io/sentinel-backend:sha-0730682"
        in (rendered / "deployment.yaml").read_text()
    )


def test_backend_up_fails_without_lb_ip(tmp_path: Path) -> None:
    stubs = _up_stubs()
    stubs["kubectl"] = "exit 0"
    run = _run_step("backend-up", "url", SAMPLE_UP_INPUTS, tmp_path, stubs)
    assert run.returncode != 0
    assert "LoadBalancer" in run.stdout
    assert len([line for line in run.log if " get svc " in line]) == 60
    assert run.outputs == {}


def test_backend_up_rejects_bad_image_tag(tmp_path: Path) -> None:
    run = _run_step(
        "backend-up",
        "apply",
        {**SAMPLE_UP_INPUTS, "image-tag": "latest"},
        tmp_path,
        _up_stubs(),
    )
    assert run.returncode != 0
    assert not any(" apply " in line for line in run.log)


def test_backend_up_requires_checkout(tmp_path: Path) -> None:
    run = _run_step(
        "backend-up",
        "apply",
        SAMPLE_UP_INPUTS,
        tmp_path,
        _up_stubs(),
        {"GITHUB_WORKSPACE": str(tmp_path)},
    )
    assert run.returncode != 0
    assert "checkout" in run.stdout
    assert run.log == []


DOWN_INPUTS = {"resource-group": "rg-test", "cluster-name": "aks-test"}


def test_backend_down_running_cluster(tmp_path: Path) -> None:
    stubs = {"az": AZ_ECHO_POWER, "kubelogin": "exit 0", "kubectl": "exit 0"}
    env = {"STUB_POWER": "Running"}
    cluster = _run_step("backend-down", "cluster", DOWN_INPUTS, tmp_path, stubs, env)
    assert cluster.returncode == 0, cluster.stdout
    assert cluster.outputs == {"stopped": "false"}
    teardown = _run_step("backend-down", "teardown", DOWN_INPUTS, tmp_path, stubs, env)
    assert teardown.returncode == 0, teardown.stdout
    joined = "\n".join(teardown.log)
    assert (
        f"kubectl -n sentinel-dev delete -f {REPO_ROOT}/azure/k8s/service.yaml --ignore-not-found"
        in joined
    )
    assert "kubectl -n sentinel-dev scale deployment/sentinel-backend --replicas=0" in joined
    assert "aks start" not in joined


def test_backend_down_stopped_cluster(tmp_path: Path) -> None:
    stubs = {"az": AZ_ECHO_POWER, "kubelogin": "exit 0", "kubectl": "exit 0"}
    run = _run_step(
        "backend-down", "cluster", DOWN_INPUTS, tmp_path, stubs, {"STUB_POWER": "Stopped"}
    )
    assert run.returncode == 0, run.stdout
    assert run.outputs == {"stopped": "true"}
    joined = "\n".join(run.log)
    assert "get-credentials" not in joined
    assert "kubelogin" not in joined
