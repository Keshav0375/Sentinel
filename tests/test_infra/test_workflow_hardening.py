"""Supply-chain and timeout hardening across every workflow and composite action (phase 8 review).

Third-party actions are pinned to a full commit SHA with a version comment, one SHA per action, the
phase-8 checkouts never persist credentials, every curl is bounded, and the callers of the shared
composite actions pass identical inputs. Nothing here calls GitHub, Azure or the backend.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))
ACTION_FILES = sorted(ACTIONS_DIR.glob("*/action.yml"))
PHASE8 = [
    WORKFLOW_DIR / name
    for name in (
        "ci_validation.yml",
        "ci_backend_deployment.yml",
        "ci_incident_response.yml",
        "ci_backend_scale.yml",
    )
]

USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", re.MULTILINE)
PIN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
# A two-part tag is accepted for upstreams that publish no patch tag (azure/use-kubelogin: v1.2).
VERSION_COMMENT = re.compile(r"#\s*v\d+\.\d+(\.\d+)?")
CURL_RE = re.compile(r"(^|[\s(|;&$])curl\s")

TOKEN_WITH = {
    "audience": "${{ vars.SENTINEL_API_AUDIENCE }}",
    "tenant": "${{ vars.AZURE_IDENTITY_TENANT_ID }}",
}
ACR_WITH = {"acr-name": "${{ vars.ACR_NAME }}"}


def _unpinned(text: str) -> list[str]:
    """The third-party `uses:` values that are not a full SHA with a `# vX.Y[.Z]` comment."""
    bad: list[str] = []
    for match in USES_RE.finditer(text):
        value, rest = match.group(1), match.group(2)
        if value.startswith(("./", "docker://")):
            continue
        if not PIN.match(value) or not VERSION_COMMENT.search(rest):
            bad.append(value)
    return bad


def _jobs(path: Path) -> dict[str, dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _all_steps(path: Path) -> list[dict[str, Any]]:
    return [step for job in _jobs(path).values() for step in job["steps"]]


def _uses_local(action: str) -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, step)
        for path in WORKFLOWS
        for step in _all_steps(path)
        if step.get("uses") == f"./.github/actions/{action}"
    ]


def test_unpinned_detector_flags_tags() -> None:
    sha = "11d5960a326750d5838078e36cf38b85af677262"

    assert _unpinned("      - uses: actions/checkout@v4\n") == ["actions/checkout@v4"]
    assert _unpinned(f"      - uses: actions/checkout@{sha}  # v4.4.0\n") == []
    assert _unpinned(f"      - uses: azure/use-kubelogin@{sha}  # v1.2\n") == []
    assert _unpinned(f"      - uses: azure/use-kubelogin@{sha}  # v1\n") == [
        f"azure/use-kubelogin@{sha}"
    ]
    assert _unpinned(f"        uses: actions/checkout@{sha}\n") == [f"actions/checkout@{sha}"]
    assert _unpinned("      - uses: ./.github/actions/backend-up\n") == []


@pytest.mark.parametrize(
    "path", [*WORKFLOWS, *ACTION_FILES], ids=lambda p: p.parent.name + "/" + p.name
)
def test_every_third_party_action_is_sha_pinned(path: Path) -> None:
    assert _unpinned(path.read_text(encoding="utf-8")) == []


def test_one_pin_per_action() -> None:
    pins: dict[str, set[str]] = {}
    for path in WORKFLOWS:
        for match in USES_RE.finditer(path.read_text(encoding="utf-8")):
            value = match.group(1)
            if value.startswith(("./", "docker://")):
                continue
            name, _, sha = value.partition("@")
            pins.setdefault(name, set()).add(sha)

    assert pins
    assert {name: shas for name, shas in pins.items() if len(shas) != 1} == {}


@pytest.mark.parametrize("path", PHASE8, ids=lambda p: p.name)
def test_checkouts_never_persist_credentials(path: Path) -> None:
    checkouts = [
        s for s in _all_steps(path) if str(s.get("uses", "")).startswith("actions/checkout@")
    ]

    assert checkouts
    for step in checkouts:
        assert step.get("with", {}).get("persist-credentials") is False, step


@pytest.mark.parametrize("path", PHASE8, ids=lambda p: p.name)
def test_every_curl_is_bounded(path: Path) -> None:
    for step in _all_steps(path):
        if "run" not in step:
            continue
        joined = step["run"].replace("\\\n", " ")
        for line in joined.splitlines():
            if line.lstrip().startswith("#") or not CURL_RE.search(line):
                continue
            assert "--connect-timeout" in line, line
            assert "--max-time" in line, line


def test_backend_token_callers_identical() -> None:
    callers = _uses_local("get-backend-token")

    assert len(callers) == 3
    for _, step in callers:
        assert step["with"] == TOKEN_WITH


def test_stable_lookup_is_shared() -> None:
    callers = _uses_local("resolve-stable-tag")

    assert sorted(path.name for path, _ in callers) == [
        "ci_backend_scale.yml",
        "ci_incident_response.yml",
    ]
    for _, step in callers:
        assert step["with"] == ACR_WITH
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        assert "--image sentinel-backend:stable" not in text, path.name
        assert "manifest list-metadata" not in text, path.name
