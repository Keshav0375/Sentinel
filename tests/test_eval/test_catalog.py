"""Tests for sentinel.catalog — the vendored scenario catalog (decision 2026-10-25)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from sentinel.catalog import CATALOG_PATH, load_catalog

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_catalog_ships_inside_the_package() -> None:
    assert CATALOG_PATH == _REPO_ROOT / "src/sentinel/catalog/branches.yaml"
    assert CATALOG_PATH.is_file()


def test_load_catalog_has_the_30_branches_in_file_order() -> None:
    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))["branches"]
    branches = [t.branch for t in load_catalog()]
    assert branches == [e["branch"] for e in raw]
    assert len(branches) == 30
    assert branches[0] == "pass/01"
    assert branches[-1] == "runtime/10"


def test_catalog_has_ten_of_each_case() -> None:
    catalog = load_catalog()
    assert Counter(t.case for t in catalog) == {"i": 10, "ii": 10, "iii": 10}
    for t in catalog:
        assert (t.expected_failed_stage is not None) == (t.case == "ii"), t.branch


def test_catalog_holds_the_demo_branches() -> None:
    by_branch = {t.branch: t for t in load_catalog()}
    expected = {
        "pass/01": ("none", "none"),
        "deployfail/03": ("deploy_failure", "rollback"),
        "runtime/01": ("runtime_error", "rollback_or_escalate"),
        "runtime/07": ("runtime_error", "rollback_or_escalate"),
    }
    for branch, pair in expected.items():
        t = by_branch[branch]
        assert (t.expected_signal_type, t.expected_resolution) == pair, branch


def test_load_catalog_is_cached() -> None:
    assert load_catalog() is load_catalog()


def test_there_is_one_catalog_copy() -> None:
    copies = sorted(
        p for d in ("src", "tests", "scripts") for p in (_REPO_ROOT / d).rglob("branches.yaml")
    )
    assert copies == [CATALOG_PATH]


def test_catalog_matches_the_deployment_repo() -> None:
    upstream = _REPO_ROOT.parent / "Sentinel-deployment/scenarios/branches.yaml"
    if not upstream.is_file():
        pytest.skip("Sentinel-deployment checkout not found next to this repo")
    assert upstream.read_bytes() == CATALOG_PATH.read_bytes()


def test_image_ships_the_catalog() -> None:
    assert "COPY src/ src/" in (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    ignore = (_REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "!src/" in ignore
