"""scripts/ops/check_site.py: the pre-publish bundle and snapshot checks (backend task 10.5).

Builds small dist/ trees under tmp_path from the 10.1 SPA fixtures; the empty-ledger case runs the
real snapshot.py to prove the first-run site is publishable. Nothing here builds the SPA.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SITE_PATH = REPO_ROOT / "scripts/ops/check_site.py"
FIXTURES = REPO_ROOT / "dashboard-web/src/fixtures"
BASE = "/Sentinel/"
GOOD_HTML = (
    '<link rel="icon" href="/Sentinel/favicon.svg">'
    '<link rel="stylesheet" href="/Sentinel/assets/index-a1.css">'
    '<script type="module" src="/Sentinel/assets/index-b2.js"></script>'
)
REFUSE = (
    "snapshot has no events but the ops-ledger branch exists; refusing to publish an empty site"
)


@pytest.fixture(scope="session")
def check_site() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ops_check_site", CHECK_SITE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ops_check_site"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def markers(check_site: ModuleType) -> set[str]:
    found: set[str] = check_site.fixture_markers(FIXTURES)
    return found


def _dist(tmp_path: Path, html: str = GOOD_HTML) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(html, encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (dist / "assets/index-a1.css").write_text("body{margin:0}", encoding="utf-8")
    (dist / "assets/index-b2.js").write_text("console.log('ok');", encoding="utf-8")
    return dist


def _snap(dist: Path, set_name: str) -> Path:
    snap = dist / "snapshot"
    shutil.copytree(FIXTURES / set_name, snap)
    return snap


def _load(path: Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return doc


def _dump(path: Path, doc: dict[str, Any]) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_markers_are_fixture_chain_ids(markers: set[str]) -> None:
    populated = _load(FIXTURES / "populated/chains.json")["chains"]

    assert len(populated) == 6
    assert markers == {c["chain_id"] for c in populated}
    assert "c_2c8f0e7a5b1d3f94" in markers


def test_good_bundle_passes(check_site: ModuleType, markers: set[str], tmp_path: Path) -> None:
    assert check_site.check_bundle(_dist(tmp_path), base=BASE, markers=markers) == []


def test_root_absolute_outside_base(
    check_site: ModuleType, markers: set[str], tmp_path: Path
) -> None:
    dist = _dist(tmp_path, GOOD_HTML + '<link rel="stylesheet" href="/assets/x.css">')

    problems = check_site.check_bundle(dist, base=BASE, markers=markers)

    assert len(problems) == 1
    assert "outside the base path /Sentinel/" in problems[0]


def test_missing_asset(check_site: ModuleType, markers: set[str], tmp_path: Path) -> None:
    dist = _dist(tmp_path, GOOD_HTML + '<script src="/Sentinel/assets/gone.js?v=1"></script>')

    problems = check_site.check_bundle(dist, base=BASE, markers=markers)

    assert problems == ["index.html: /Sentinel/assets/gone.js?v=1 is not in the build"]


def test_snapshot_preload_refs_allowed(
    check_site: ModuleType, markers: set[str], tmp_path: Path
) -> None:
    """snapshot/ is added at publish time: its preloads are not "in the build" yet."""
    html = (
        '<script type="module" src="/Sentinel/assets/index-b2.js"></script>'
        '<link rel="preload" as="fetch" crossorigin href="/Sentinel/snapshot/meta.json">'
    )
    dist = _dist(tmp_path, html)
    assert not (dist / "snapshot").exists()

    assert check_site.check_bundle(dist, base=BASE, markers=markers) == []

    relative = html + '<link rel="preload" as="fetch" href="snapshot/meta.json">'
    (dist / "index.html").write_text(relative, encoding="utf-8")
    assert check_site.check_bundle(dist, base=BASE, markers=markers) == [
        "index.html: snapshot/meta.json is relative; expected /Sentinel/…"
    ]


def test_relative_ref(check_site: ModuleType, markers: set[str], tmp_path: Path) -> None:
    dist = _dist(tmp_path, GOOD_HTML + '<script src="assets/x.js"></script>')

    problems = check_site.check_bundle(dist, base=BASE, markers=markers)

    assert len(problems) == 1
    assert "is relative" in problems[0]


def test_external_refs_skipped(check_site: ModuleType, markers: set[str], tmp_path: Path) -> None:
    extra = '<a href="https://github.com/Keshav0375/Sentinel">x</a><a href="#main">y</a>'
    dist = _dist(tmp_path, GOOD_HTML + extra)

    assert check_site.check_bundle(dist, base=BASE, markers=markers) == []


def test_no_assets_ref(check_site: ModuleType, markers: set[str], tmp_path: Path) -> None:
    dist = _dist(tmp_path, '<link rel="icon" href="/Sentinel/favicon.svg">')

    problems = check_site.check_bundle(dist, base=BASE, markers=markers)

    assert len(problems) == 1
    assert "references no /Sentinel/assets/ file" in problems[0]


def test_missing_index(check_site: ModuleType, markers: set[str], tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()

    assert check_site.check_bundle(dist, base=BASE, markers=markers) == ["index.html is missing"]


def test_fixture_id_in_bundle(check_site: ModuleType, markers: set[str], tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    (dist / "assets/index-b2.js").write_text('const x = "c_2c8f0e7a5b1d3f94";', encoding="utf-8")

    problems = check_site.check_bundle(dist, base=BASE, markers=markers)

    assert problems == ["assets/index-b2.js: contains fixture chain id c_2c8f0e7a5b1d3f94"]


def test_fixture_path_in_bundle(check_site: ModuleType, markers: set[str], tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    leaked = dist / "src/fixtures/populated/meta.json"
    leaked.parent.mkdir(parents=True)
    leaked.write_text("{}", encoding="utf-8")

    problems = check_site.check_bundle(dist, base=BASE, markers=markers)

    assert problems == ["src/fixtures/populated/meta.json: source or fixture path in the build"]


def test_snapshot_dir_not_scanned_for_markers(
    check_site: ModuleType, markers: set[str], tmp_path: Path
) -> None:
    dist = _dist(tmp_path)
    _snap(dist, "populated")

    assert check_site.check_bundle(dist, base=BASE, markers=markers) == []


def test_snapshot_populated_valid(check_site: ModuleType, tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _snap(dist, "populated")

    assert check_site.check_snapshot(dist, require_events=True) == []


def test_snapshot_empty_valid_and_guard(check_site: ModuleType, tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _snap(dist, "empty")

    assert check_site.check_snapshot(dist, require_events=False) == []
    assert check_site.check_snapshot(dist, require_events=True) == [REFUSE]


def test_snapshot_missing(check_site: ModuleType, tmp_path: Path) -> None:
    dist = _dist(tmp_path)

    assert check_site.check_snapshot(dist, require_events=False) == ["snapshot/ is missing"]
    snap = _snap(dist, "populated")
    (snap / "metrics.json").unlink()
    assert check_site.check_snapshot(dist, require_events=False) == [
        "snapshot/metrics.json: missing"
    ]


def test_snapshot_invalid(check_site: ModuleType, tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    snap = _snap(dist, "populated")
    meta = _load(snap / "meta.json")
    meta["schema"] = "x"
    _dump(snap / "meta.json", meta)
    chains = _load(snap / "chains.json")
    chains["file"] = "meta"
    _dump(snap / "chains.json", chains)
    (snap / "metrics.json").write_text("{not json", encoding="utf-8")

    problems = check_site.check_snapshot(dist, require_events=False)

    assert any(p.startswith("snapshot/meta.json: ") for p in problems), problems
    assert any(
        p.startswith("snapshot/chains.json: ") and "expected 'chains'" in p for p in problems
    ), problems
    assert "snapshot/metrics.json: not valid JSON" in problems


def test_snapshot_missing_entity(check_site: ModuleType, tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    snap = _snap(dist, "populated")
    cid = _load(snap / "chains.json")["chains"][0]["chain_id"]
    (snap / "entities" / f"{cid}.json").unlink()

    problems = check_site.check_snapshot(dist, require_events=False)

    assert problems == [f"snapshot/entities/{cid}.json: missing"]


def test_snapshot_secret(check_site: ModuleType, tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    snap = _snap(dist, "populated")
    chains = _load(snap / "chains.json")
    chains["chains"][0]["title"] = "ghp_" + "A" * 36
    _dump(snap / "chains.json", chains)

    problems = check_site.check_snapshot(dist, require_events=False)

    assert any("secret-like value at" in p for p in problems), problems
    assert not any("ghp_" in p for p in problems), problems


def test_cli(check_site: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dist = _dist(tmp_path)
    fixtures = str(FIXTURES)

    assert check_site.main([str(dist), "--fixtures", fixtures]) == 0
    assert capsys.readouterr().out.startswith("check-site: ok")

    _snap(dist, "populated")
    assert (
        check_site.main([str(dist), "--fixtures", fixtures, "--snapshot", "--require-events"]) == 0
    )
    assert "snapshot" in capsys.readouterr().out

    (dist / "index.html").write_text('<script src="/assets/x.js"></script>', encoding="utf-8")
    assert check_site.main([str(dist), "--fixtures", fixtures]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err
    assert all(line.startswith("check-site: ") for line in err)

    with pytest.raises(SystemExit) as bad_base:
        check_site.main([str(dist), "--fixtures", fixtures, "--base", "Sentinel"])
    assert bad_base.value.code == 2
    with pytest.raises(SystemExit) as no_snapshot:
        check_site.main([str(dist), "--fixtures", fixtures, "--require-events"])
    assert no_snapshot.value.code == 2
    capsys.readouterr()

    good = _dist(tmp_path / "again")
    assert check_site.main([str(good), "--fixtures", str(tmp_path / "nope")]) == 1
    assert "the no-fixture check would be void" in capsys.readouterr().err

    assert check_site.main([str(tmp_path / "missing"), "--fixtures", fixtures]) == 1
    assert "is not a directory" in capsys.readouterr().err


def test_empty_ledger_snapshot_is_publishable(check_site: ModuleType, tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    status = tmp_path / "ops-status.json"
    status.write_text('{"github": "skipped", "langfuse": "skipped"}\n', encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            "scripts/ops/snapshot.py",
            "--ledger",
            str(ledger_dir),
            "--out",
            str(dist / "snapshot"),
            "--status",
            str(status),
            "--now",
            "2026-10-25T12:00:00Z",
        ],
        cwd=REPO_ROOT,
        check=True,
        timeout=60,
        capture_output=True,
    )

    assert check_site.check_snapshot(dist, require_events=False) == []
    chains = _load(dist / "snapshot/chains.json")
    assert chains["chains"] == []
    assert chains["chains"] == _load(FIXTURES / "empty/chains.json")["chains"]
    meta = _load(dist / "snapshot/meta.json")
    assert meta["event_count"] == 0
    assert meta["chain_count"] == 0
    assert meta["ledger_head"] is None
    assert meta["first_event_at"] is None
    assert meta["sources"] == {"github": "skipped", "langfuse": "skipped"}
    metrics = _load(dist / "snapshot/metrics.json")
    assert set(metrics) == set(_load(FIXTURES / "empty/metrics.json"))
    assert check_site.check_snapshot(dist, require_events=True) == [REFUSE]
