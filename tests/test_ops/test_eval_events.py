"""eval.scored from the eval report: rules.eval_events and `emit.py eval` (decision 2026-10-28).

The fixture is a recorded report (to_json_dict(build_report(...)) with generated_at fixed): two
deployfail/05 items (older and newer), an unjudged pass/01, a judge_failed runtime/01, a
judge_conflict deployfail/01 and an item whose branch is not a catalog scenario. rules.py and
emit.py are scripts, loaded with importlib as test_rules does. Nothing here touches git or the
network.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from sentinel.catalog import catalog_branches
from sentinel.models.eval_result import GroundTruthResult, TrajectoryScore

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts/ops"
EMIT = OPS_DIR / "emit.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures/eval/eval_report.json"
SOURCE = "gha:Keshav0375/Sentinel/ci_eval.yml"
RUN_URL = "https://github.com/Keshav0375/Sentinel/actions/runs/123"
NEWER = "22222222-2222-4222-8222-222222222222"
OLDER = "11111111-1111-4111-8111-111111111111"
REPORT_KEYS = {"generated_at", "summary", "dimension_stats", "results", "ground_truth"}


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: the frozen dataclasses resolve their postponed annotations here.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def rules() -> ModuleType:
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    return _load("eval_ops_rules", OPS_DIR / "rules.py")


@pytest.fixture(scope="session")
def emit() -> ModuleType:
    return _load("eval_ops_emit", OPS_DIR / "emit.py")


@pytest.fixture(scope="session")
def snapshot() -> ModuleType:
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    return _load("eval_ops_snapshot", OPS_DIR / "snapshot.py")


@cache
def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _report() -> dict[str, Any]:
    report: dict[str, Any] = json.loads(_fixture_text())
    return report


def _by_state(rules: ModuleType, report: dict[str, Any] | None = None) -> dict[str, Any]:
    selection = rules.eval_events(report or _report(), run_id=123, run_attempt=2)
    return {spec.state: spec for spec in selection.events}


def _item(branch: str, item_id: str, **extra: Any) -> dict[str, Any]:
    checks = dict.fromkeys(("fired", "classified", "correlated", "revert_target"), 1.0)
    return {
        "item_id": item_id,
        "branch": branch,
        "case": "ii",
        "checks": checks,
        "judge_conflict": False,
        "incident_id": None,
        "judge_failed": False,
        "created_at": None,
        **extra,
    }


def _inline(
    items: list[dict[str, Any]], results: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "generated_at": "2026-10-28T09:30:00Z",
        "results": results or [],
        "ground_truth": {"items": items},
    }


# ── rules.eval_events ─────────────────────────────────────────────────────────


def test_fixture_is_the_report_shape() -> None:
    report = _report()
    assert set(report) == REPORT_KEYS
    items = [i for i in report["ground_truth"]["items"] if i["branch"] != "feature/x"]
    assert len(items) == 5
    for item in items:
        GroundTruthResult.model_validate(item)
    for result in report["results"]:
        TrajectoryScore.model_validate(result)


def test_newest_item_per_branch(rules: ModuleType) -> None:
    selection = rules.eval_events(_report(), run_id=123, run_attempt=2)
    assert [s.state for s in selection.events] == [
        "pass/01",
        "deployfail/01",
        "deployfail/05",
        "runtime/01",
    ]
    (df05,) = [s for s in selection.events if s.state == "deployfail/05"]
    assert df05.chain["incident_id"] == NEWER
    assert selection.older == {"deployfail/05": 1}
    assert selection.invalid == 1


def test_event_shape(rules: ModuleType) -> None:
    spec = _by_state(rules)["deployfail/05"]
    assert spec.kind == "eval.scored"
    assert spec.source == SOURCE
    assert spec.source_id == "123.2"
    assert spec.state == "deployfail/05"
    assert spec.chain == {"run_id": 123, "scenario": "deployfail/05", "incident_id": NEWER}
    assert spec.data == {
        "case": "ii",
        "checks": {"fired": 1.0, "classified": 1.0, "correlated": 1.0, "revert_target": 1.0},
        "judge": {
            "triage_accuracy": 0.9,
            "root_cause_correctness": 0.8,
            "tool_efficiency": 0.7,
            "mttr": 0.6,
            "remediation_safety": 1.0,
        },
        "total_score": 0.8,
        "judge_model": "openai/gpt-4.1",
        "judge_conflict": False,
        "judge_failed": False,
        "run_url": RUN_URL,
    }
    assert spec.build()["ts"] == "2026-10-28T09:30:00Z"


def test_every_event_is_schema_valid(rules: ModuleType) -> None:
    specs = _by_state(rules)
    assert len(specs) == 4
    for spec in specs.values():
        event = spec.build()
        assert event["kind"] == "eval.scored"
        assert event["data"]["run_url"] == RUN_URL


def test_flagged_items_carry_flags_not_scores(rules: ModuleType) -> None:
    specs = _by_state(rules)
    failed = specs["runtime/01"].data
    assert failed["judge_failed"] is True
    assert failed["judge_conflict"] is False
    assert failed["judge_model"] == "openai/gpt-4.1"
    assert "judge" not in failed
    assert "total_score" not in failed
    conflict = specs["deployfail/01"].data
    assert conflict["judge_conflict"] is True
    assert conflict["judge_failed"] is False
    assert conflict["judge_model"] == "openai/gpt-4.1"
    assert "judge" not in conflict
    assert "total_score" not in conflict


def test_unjudged_item(rules: ModuleType) -> None:
    spec = _by_state(rules)["pass/01"]
    assert spec.data["checks"] == dict.fromkeys(
        ("fired", "classified", "correlated", "revert_target"), 1.0
    )
    for key in ("judge_model", "judge", "total_score"):
        assert key not in spec.data
    assert spec.chain == {"run_id": 123, "scenario": "pass/01"}


def test_created_at_ties_and_missing(rules: ModuleType) -> None:
    def chosen(items: list[dict[str, Any]]) -> str:
        (spec,) = rules.eval_events(_inline(items), run_id=1, run_attempt=1).events
        case: str = spec.data["case"]
        return case

    ts = "2026-10-27T12:00:00Z"
    # None vs set: the dated item wins, whichever comes first.
    assert chosen([_item("pass/02", "a", case="i", created_at=ts), _item("pass/02", "b")]) == "i"
    assert chosen([_item("pass/02", "b"), _item("pass/02", "a", case="i", created_at=ts)]) == "i"
    # Both None: the later index wins.
    assert chosen([_item("pass/02", "a", case="i"), _item("pass/02", "b", case="iii")]) == "iii"
    # Equal timestamps: the later index wins.
    assert (
        chosen(
            [
                _item("pass/02", "a", case="i", created_at=ts),
                _item("pass/02", "b", case="iii", created_at="2026-10-27T12:00:00+00:00"),
            ]
        )
        == "iii"
    )
    selection = rules.eval_events(
        _inline([_item("pass/02", "a"), _item("pass/02", "b"), _item("pass/03", "c")]),
        run_id=1,
        run_attempt=1,
    )
    assert selection.older == {"pass/02": 1}


def test_bad_values_are_dropped_not_fatal(rules: ModuleType) -> None:
    incident = "not-a-uuid"
    item = _item(
        "runtime/04",
        "a",
        case="iv",
        incident_id=incident,
        checks={"fired": 1.5, "classified": 1.0, "correlated": 0.0, "revert_target": 1.0},
    )
    dims = [
        {"dimension": "triage_accuracy", "score": True, "reasoning": "r"},
        {"dimension": "mttr", "score": 0.5, "reasoning": "r"},
    ]
    result = {
        "incident_id": incident,
        "scenario_id": "runtime/04",
        "dimension_scores": dims,
        "total_score": 0.5,
        "judge_model": "bad model",
        "judge_conflict": False,
        "judge_failed": False,
    }
    (spec,) = rules.eval_events(_inline([item], [result]), run_id=1, run_attempt=1).events
    assert "incident_id" not in spec.chain
    assert "case" not in spec.data
    assert "judge_model" not in spec.data
    assert spec.data["checks"] == {"classified": 1.0, "correlated": 0.0, "revert_target": 1.0}
    # The non-UUID incident still joins the judge result: its valid fields are kept.
    assert spec.data["judge"] == {"mttr": 0.5}
    assert spec.data["total_score"] == 0.5
    spec.build()


def test_judge_join_needs_the_same_scenario(rules: ModuleType) -> None:
    item = _item("runtime/04", "a", incident_id=NEWER)
    result = {
        "incident_id": NEWER,
        "scenario_id": "runtime/05",
        "dimension_scores": [],
        "total_score": 0.5,
        "judge_model": "openai/gpt-4.1",
    }
    (spec,) = rules.eval_events(_inline([item], [result]), run_id=1, run_attempt=1).events
    assert "judge_model" not in spec.data
    assert "total_score" not in spec.data


@pytest.mark.parametrize("report", [{}, {"ground_truth": []}, {"results": "x"}])
def test_shapes_that_are_not_reports(rules: ModuleType, report: dict[str, Any]) -> None:
    selection = rules.eval_events(report, run_id=1, run_attempt=1)
    assert selection.events == []
    assert selection.invalid == 0
    assert selection.older == {}


def test_unusable_items_warn(rules: ModuleType) -> None:
    warnings: list[str] = []
    report = _inline(["junk", _item("main", "a"), _item("pass/01", "b")])  # type: ignore[list-item]
    selection = rules.eval_events(report, run_id=1, run_attempt=1, warn=warnings.append)
    assert [s.state for s in selection.events] == ["pass/01"]
    assert selection.invalid == 2
    assert warnings == ["an eval item has no usable branch"] * 2


def test_event_ids_stable_and_per_scenario(rules: ModuleType) -> None:
    first = [s.event_id for s in rules.eval_events(_report(), run_id=123, run_attempt=2).events]
    again = [s.event_id for s in rules.eval_events(_report(), run_id=123, run_attempt=2).events]
    retry = [s.event_id for s in rules.eval_events(_report(), run_id=123, run_attempt=3).events]
    assert first == again
    assert len(set(first)) == 4
    assert not set(first) & set(retry)


def test_eval_constants_match_snapshot(rules: ModuleType, snapshot: ModuleType) -> None:
    assert rules.EVAL_CHECKS == snapshot.CHECK_NAMES
    assert rules.EVAL_DIMENSIONS == snapshot.JUDGE_NAMES


def test_eval_slots_cover_the_catalog(emit: ModuleType) -> None:
    slots = emit.SLOTS["eval"]
    assert len(slots) == 30
    assert set(slots) == {b.replace("/", "-") for b in catalog_branches()}


def test_report_is_not_mutated(rules: ModuleType) -> None:
    report = _report()
    before = copy.deepcopy(report)
    rules.eval_events(report, run_id=123, run_attempt=2)
    assert report == before


# ── emit.py eval ──────────────────────────────────────────────────────────────


def _run_emit(report: Path, summary: Path) -> tuple[int, dict[str, str], str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_REPOSITORY": "Keshav0375/Sentinel",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "EVAL_REPORT": str(report),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    proc = subprocess.run(
        [sys.executable, str(EMIT), "eval"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    outputs = dict(line.partition("=")[::2] for line in proc.stdout.splitlines())
    return proc.returncode, outputs, proc.stdout, proc.stderr


def test_emit_eval_prints_every_slot(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    rc, outputs, stdout, stderr = _run_emit(FIXTURE, summary)
    assert rc == 0, stderr
    assert len(stdout.splitlines()) == 150
    assert outputs["deployfail-05-kind"] == "eval.scored"
    assert outputs["deployfail-05-state"] == "deployfail/05"
    assert outputs["deployfail-05-source-id"] == "123.2"
    assert json.loads(outputs["deployfail-05-chain"])["incident_id"] == NEWER
    assert json.loads(outputs["deployfail-05-data"])["total_score"] == 0.8
    assert outputs["pass-02-kind"] == ""
    filled = sorted(k for k, v in outputs.items() if k.endswith("-kind") and v)
    assert filled == [
        "deployfail-01-kind",
        "deployfail-05-kind",
        "pass-01-kind",
        "runtime-01-kind",
    ]
    text = summary.read_text(encoding="utf-8")
    assert "### eval.scored" in text
    assert "emitted: 4" in text
    assert "older items not emitted: 1 (deployfail/05: 1)" in text
    assert "unusable branch: 1" in text
    assert OLDER not in stdout


def test_emit_eval_missing_report(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    rc, outputs, stdout, stderr = _run_emit(missing, tmp_path / "summary.md")
    assert rc == 0
    kinds = {k: v for k, v in outputs.items() if k.endswith("-kind")}
    assert len(kinds) == 30
    assert set(kinds.values()) == {""}
    assert "::warning::ops-emit: EVAL_REPORT is unreadable" in stderr
    assert str(missing) not in stdout + stderr


def test_emit_eval_not_json(tmp_path: Path) -> None:
    report = tmp_path / "eval_report.json"
    report.write_text("[]", encoding="utf-8")
    rc, outputs, _, stderr = _run_emit(report, tmp_path / "summary.md")
    assert rc == 0
    assert {v for k, v in outputs.items() if k.endswith("-kind")} == {""}
    assert "::warning::ops-emit: EVAL_REPORT is not a JSON object" in stderr
