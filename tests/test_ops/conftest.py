"""Shared fixtures for scripts/ops (backend task 10.1).

ledger.py is a script, not a package, so it is loaded with importlib. Every git test writes to a
local bare repo under tmp_path; nothing here talks to GitHub.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = REPO_ROOT / "scripts/ops/ledger.py"
FIXTURES = REPO_ROOT / "dashboard-web/src/fixtures"

DEFAULT_SOURCE = "gha:Keshav0375/Sentinel-deployment/deploy.yml"
DEFAULT_SOURCE_ID = "18800000202.1"
DEFAULT_TS = "2026-10-25T12:00:00Z"
DEFAULT_RECORDED_AT = datetime(2026, 10, 25, 12, 0, 5, tzinfo=UTC)


@pytest.fixture(scope="session")
def ledger() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ops_ledger", LEDGER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: the frozen dataclass resolves its postponed annotations here.
    sys.modules["ops_ledger"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def event_schema() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "scripts/ops/schema/event.v1.json").read_text())


@pytest.fixture(scope="session")
def snapshot_schema() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "scripts/ops/schema/snapshot.v1.json").read_text())


@pytest.fixture
def make_event(ledger: ModuleType) -> Callable[..., dict[str, Any]]:
    def factory(kind: str = "deploy.failed", **overrides: Any) -> dict[str, Any]:
        args: dict[str, Any] = {
            "kind": kind,
            "source": DEFAULT_SOURCE,
            "source_id": DEFAULT_SOURCE_ID,
            "ts": DEFAULT_TS,
            "recorded_at": DEFAULT_RECORDED_AT,
            "chain": {"sha": "4f1d2a9"},
            "data": {
                "run_url": "https://github.com/Keshav0375/Sentinel-deployment/actions/runs/18800000202",
                "failed_stage": "verify",
            },
        }
        args.update(overrides)
        event: dict[str, Any] = ledger.build_event(**args)
        return event

    return factory


@pytest.fixture(scope="session")
def git_bin() -> str:
    path = shutil.which("git")
    if path is None:
        pytest.skip("git not on PATH")
    return path


def clean_env() -> dict[str, str]:
    """os.environ without inherited GIT_* (a git hook or an IDE may set GIT_DIR)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class BareRemote:
    """A local bare repo standing in for the GitHub ledger remote."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.uri = path.as_uri()

    def git(self, *args: str) -> str:
        return self.git_bytes(*args).decode().strip()

    def git_bytes(self, *args: str) -> bytes:
        proc = subprocess.run(
            ["git", "--git-dir", str(self.path), *args],
            env=clean_env(),
            capture_output=True,
            check=True,
        )
        return proc.stdout

    def files(self, ref: str = "ops-ledger") -> set[str]:
        return set(self.git("ls-tree", "-r", "--name-only", ref).splitlines())

    def events(self, ref: str = "ops-ledger") -> list[str]:
        return sorted(p for p in self.files(ref) if p.startswith("events/"))


@pytest.fixture
def remote(tmp_path: Path, git_bin: str) -> BareRemote:
    path = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(path)], env=clean_env(), check=True, capture_output=True
    )
    return BareRemote(path)


@pytest.fixture
def env_without_git() -> dict[str, str]:
    return clean_env()
