"""Tests for eval/runner.py — the read-only scorer of scenario runs held in a dataset."""

from __future__ import annotations

import ast
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinel.api.eval_report import make_eval_report_router
from sentinel.catalog import CATALOG_PATH
from sentinel.config import Settings
from sentinel.eval import runner
from sentinel.eval.judge import make_judge_client
from sentinel.eval.runner import (
    DATASET_NAME,
    BranchTruth,
    ScenarioRun,
    bind_culprit,
    fetch_dataset_items,
    ground_truth_checks,
    load_branches,
    run_eval,
    same_deploy,
)
from sentinel.exceptions import EvalError, SentinelMemoryError
from sentinel.models.eval_result import GroundTruthCheck, TrajectoryScore
from sentinel.models.pipeline import PipelineResult
from sentinel.models.tools import EscalationContext, RollbackSpec, TargetDeploy

FIXTURE = CATALOG_PATH  # the vendored catalog (decision 2026-10-25): no second copy
_DEPLOYED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _settings() -> Settings:
    return Settings(_env_file=None, sentinel_fake_llm=True)  # type: ignore[call-arg]


def _judge_model() -> str:
    return make_judge_client(_settings())[0]


@dataclass
class FakeSpan:
    trace_id: str


@dataclass
class FakeItem:
    id: str
    input: Any
    runs: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    @contextmanager
    def run(
        self,
        *,
        run_name: str,
        run_metadata: Any | None = None,
        run_description: str | None = None,
    ) -> Iterator[FakeSpan]:
        self.runs.append({"run_name": run_name, "run_metadata": run_metadata})
        yield FakeSpan(f"trace-{self.id}")


@dataclass
class FakeStore:
    missing: set[str] = field(default_factory=set[str])
    failing: set[str] = field(default_factory=set[str])
    calls: list[tuple[str, TrajectoryScore]] = field(
        default_factory=list[tuple[str, TrajectoryScore]]
    )

    async def store_eval_score(self, incident_id: str, score: TrajectoryScore) -> bool:
        self.calls.append((incident_id, score))
        if incident_id in self.failing:
            raise SentinelMemoryError("down")
        return incident_id not in self.missing


def _primary_id(item: FakeItem) -> str:
    """The first incident carries the pipeline result in every _dataset item."""
    return str(item.input["incidents"][0]["incident_id"])


class ThreadItem(FakeItem):
    threads: list[int] = []

    @contextmanager
    def run(
        self,
        *,
        run_name: str,
        run_metadata: Any | None = None,
        run_description: str | None = None,
    ) -> Iterator[FakeSpan]:
        self.threads.append(threading.get_ident())
        yield FakeSpan(f"trace-{self.id}")


class FailingClient:
    async def complete(self, **kw: Any) -> str:
        raise RuntimeError("down")


class BrokenItem(FakeItem):
    @contextmanager
    def run(
        self,
        *,
        run_name: str,
        run_metadata: Any | None = None,
        run_description: str | None = None,
    ) -> Iterator[FakeSpan]:
        raise RuntimeError("down")
        yield FakeSpan("never")


def _rollback(pr: int, sha: str, models_used: list[str] | None = None) -> dict[str, Any]:
    incident_id = str(uuid.uuid4())
    return PipelineResult(
        incident_id=incident_id,
        status="rollback_proposed",
        resolution_type="rollback",
        rollback_spec=RollbackSpec(
            incident_id=incident_id,
            root_cause="x",
            evidence_summary="e",
            confidence=0.9,
            justification="j",
            target_deploy=TargetDeploy(pr_number=pr, commit_sha=sha, deployed_at=_DEPLOYED_AT),
        ),
        confidence=0.9,
        root_cause="x",
        reflexion_loops=0,
        tool_calls_used=2,
        tokens_used=100,
        models_used=models_used or ["anthropic/claude-haiku-4-5"],
    ).model_dump(mode="json")


def _escalated(models_used: list[str] | None = None) -> dict[str, Any]:
    return PipelineResult(
        incident_id=str(uuid.uuid4()),
        status="escalated",
        resolution_type="escalated",
        escalation=EscalationContext(hypothesis="h", evidence=["e"], confidence=0.4, missing=["m"]),
        confidence=0.4,
        root_cause="h",
        reflexion_loops=1,
        tool_calls_used=3,
        tokens_used=120,
        models_used=models_used or ["anthropic/claude-haiku-4-5"],
    ).model_dump(mode="json")


def _incident(
    signal_type: str, result: dict[str, Any] | None, correlated: str | None
) -> dict[str, Any]:
    return {
        "incident_id": str(uuid.uuid4()),
        "signal_type": signal_type,
        "correlated_deploy": correlated,
        "pipeline_result": result,
        "trajectory": {"spans": []},
    }


def _catalog() -> dict[str, BranchTruth]:
    return load_branches(FIXTURE)


def _run(
    branch: str, pr: int | None, sha: str | None, incidents: list[dict[str, Any]]
) -> ScenarioRun:
    return ScenarioRun.model_validate(
        {"branch": branch, "pr_number": pr, "merge_sha": sha, "incidents": incidents}
    )


def _checks(run: ScenarioRun) -> dict[str, float]:
    truth = _catalog()[run.branch]
    out = ground_truth_checks(truth, run, bind_culprit(truth, run))
    return {str(k): v for k, v in out.items()}


def _item(
    item_id: str, branch: str, pr: int | None, sha: str | None, incidents: list[Any]
) -> FakeItem:
    return FakeItem(
        item_id, {"branch": branch, "pr_number": pr, "merge_sha": sha, "incidents": incidents}
    )


def _dataset() -> list[FakeItem]:
    return [
        _item("pass-01", "pass/01", None, None, []),
        _item(
            "df-05",
            "deployfail/05",
            41,
            "abc1234",
            [
                _incident("deploy_failure", _rollback(41, "abc1234def"), "pr-41-abc1234"),
                _incident("runtime_error", _escalated(), "pr-41-abc1234"),
            ],
        ),
        _item(
            "rt-01",
            "runtime/01",
            42,
            "bbb2222",
            [_incident("runtime_error", _escalated(), "pr-42-bbb2222")],
        ),
        _item(
            "df-01",
            "deployfail/01",
            43,
            "ccc3333",
            [_incident("deploy_failure", _rollback(40, "0000000"), "pr-40-0000000")],
        ),
    ]


# ── Static guarantees ─────────────────────────────────────────────────────────


def test_runner_never_imports_actors() -> None:
    src = Path(runner.__file__).read_text(encoding="utf-8")
    banned = ("sentinel.agents", "sentinel.tools")
    banned += ("sentinel.memory", "httpx", "asyncpg")
    for node in ast.walk(ast.parse(src)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert not any(name == b or name.startswith(f"{b}.") for b in banned), name


# ── Catalog ───────────────────────────────────────────────────────────────────


def test_fixture_is_the_vendored_catalog() -> None:
    catalog = _catalog()
    assert len(catalog) == 30
    cases = [t.case for t in catalog.values()]
    assert {c: cases.count(c) for c in ("i", "ii", "iii")} == {"i": 10, "ii": 10, "iii": 10}
    assert catalog["deployfail/05"].also_expected == ("runtime_error",)
    assert catalog["pass/01"].expected_culprit == "none"


def test_load_branches_rejects_bad_files(tmp_path: Path) -> None:
    entry = (
        "- branch: a/01\n  case: i\n  fault: f\n  expected_signal_type: {sig}\n"
        "  expected_resolution: none\n  also_expected: []\n  expected_culprit: none\n"
    )
    files = {
        "invalid.yaml": "branches: [unclosed",
        "nokey.yaml": "other: 1\n",
        "badtype.yaml": "branches:\n" + entry.format(sig="maybe"),
        "dup.yaml": "branches:\n" + entry.format(sig="none") + entry.format(sig="none"),
    }
    for name, text in files.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        with pytest.raises(EvalError):
            load_branches(path)


def test_bind_culprit() -> None:
    catalog = _catalog()
    assert bind_culprit(catalog["deployfail/05"], _run("deployfail/05", 41, "abc1234", [])) == (
        "pr-41-abc1234"
    )
    assert bind_culprit(catalog["pass/01"], _run("pass/01", None, None, [])) is None
    with pytest.raises(EvalError):
        bind_culprit(catalog["deployfail/05"], _run("deployfail/05", 41, None, []))


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("pr-41-abc1234def", True),
        ("pr-41-ABC1234", True),
        ("pr-42-abc1234", False),
        ("pr-41-abc9999", False),
        (None, False),
        ("v1", False),
    ],
)
def test_same_deploy(version: str | None, expected: bool) -> None:
    assert same_deploy(version, "pr-41-abc1234") is expected


# ── Checks ────────────────────────────────────────────────────────────────────

ALL_PASS = {"fired": 1.0, "classified": 1.0, "correlated": 1.0, "revert_target": 1.0}


def test_checks_clean_pass_true_negative() -> None:
    assert _checks(_run("pass/01", None, None, [])) == ALL_PASS


def test_checks_clean_pass_false_positive() -> None:
    run = _run("pass/01", None, None, [_incident("runtime_error", _escalated(), None)])
    assert _checks(run) == {
        "fired": 0.0,
        "classified": 0.0,
        "correlated": 0.0,
        "revert_target": 1.0,
    }


def test_checks_deploy_failure_with_second_alert() -> None:
    run = _run(
        "deployfail/05",
        41,
        "abc1234",
        [
            _incident("deploy_failure", _rollback(41, "abc1234def"), "pr-41-abc1234"),
            _incident("runtime_error", _escalated(), "pr-41-abc1234"),
        ],
    )
    assert _checks(run) == ALL_PASS


def test_checks_unexpected_signal_type_fails_classified() -> None:
    run = _run(
        "deployfail/01",
        43,
        "ccc3333",
        [_incident("runtime_error", _escalated(), "pr-43-ccc3333")],
    )
    assert _checks(run)["classified"] == 0.0


def test_checks_wrong_revert_target() -> None:
    run = _run(
        "deployfail/01",
        43,
        "ccc3333",
        [_incident("deploy_failure", _rollback(40, "0000000"), "pr-40-0000000")],
    )
    assert _checks(run) == {
        "fired": 1.0,
        "classified": 1.0,
        "correlated": 0.0,
        "revert_target": 0.0,
    }


def test_checks_runtime_escalation_passes_revert_target() -> None:
    incidents = [_incident("runtime_error", _escalated(), "pr-42-bbb2222")]
    assert _checks(_run("runtime/01", 42, "bbb2222", incidents)) == ALL_PASS
    unfinished = [_incident("runtime_error", None, "pr-42-bbb2222")]
    assert _checks(_run("runtime/01", 42, "bbb2222", unfinished))["revert_target"] == 0.0


def test_checks_uncorrelated_incident() -> None:
    run = _run(
        "deployfail/05",
        41,
        "abc1234",
        [
            _incident("deploy_failure", _rollback(41, "abc1234def"), "pr-41-abc1234"),
            _incident("runtime_error", _escalated(), None),
        ],
    )
    assert _checks(run)["correlated"] == 0.0


# ── run_eval ──────────────────────────────────────────────────────────────────


async def _fixture_run(
    tmp_path: Path,
) -> tuple[Any, list[FakeItem], MagicMock, MagicMock, MagicMock]:
    items = _dataset()
    with (
        patch("sentinel.eval.runner.flush_traces") as flush,
        patch("sentinel.eval.runner.push_score") as ps,
        patch("sentinel.eval.judge.push_score") as pj,
    ):
        run = await run_eval(
            branches_path=FIXTURE,
            items=items,
            settings=_settings(),
            reports_dir=tmp_path,
            run_name="r1",
        )
    return run, items, flush, ps, pj


async def test_run_eval_fixture_dataset_fake_llm(tmp_path: Path) -> None:
    run, items, flush, ps, pj = await _fixture_run(tmp_path)

    assert len(run.results) == 4
    report = run.report
    assert report.items_scored == 4
    rates = {str(cs.check): cs.pass_rate for cs in report.check_stats}
    assert rates == {"fired": 1.0, "classified": 1.0, "correlated": 0.75, "revert_target": 0.75}
    assert report.scenarios_run == 3  # pass/01 has no incident, so no judge score
    assert report.avg_total_score == pytest.approx(0.5)
    for item in items:
        branch = item.input["branch"]
        assert item.runs == [{"run_name": "r1", "run_metadata": {"branch": branch}}]

    assert ps.call_count == 16
    for call in ps.call_args_list:
        trace_id, name, value = call.args
        assert trace_id.startswith("trace-")
        assert name in {str(c) for c in GroundTruthCheck}
        assert value in (0.0, 1.0)
    assert {c.args[0] for c in ps.call_args_list} == {f"trace-{i.id}" for i in items}
    assert pj.call_count == 15
    assert "trace-pass-01" not in {c.args[0] for c in pj.call_args_list}
    flush.assert_called_once()


async def test_run_eval_writes_the_report_the_api_serves(tmp_path: Path) -> None:
    await _fixture_run(tmp_path)
    report_path = tmp_path / "eval_report.json"
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["summary"]["dataset"] == DATASET_NAME == "sentinel-scenario-runs"
    assert data["summary"]["run_name"] == "r1"
    assert len(data["ground_truth"]["checks"]) == 4
    assert len(data["ground_truth"]["items"]) == 4
    assert len(data["results"]) == 3
    assert (tmp_path / "eval_report.md").exists()

    app = FastAPI()
    app.include_router(make_eval_report_router(report_path))
    response = TestClient(app).get("/api/eval-report")
    assert response.status_code == 200
    assert response.json()["summary"] == data["summary"]


async def test_judge_conflict_item_excluded_and_not_pushed(tmp_path: Path) -> None:
    ok = _dataset()[1]
    conflict = _item(
        "rt-conflict",
        "runtime/01",
        42,
        "bbb2222",
        [_incident("runtime_error", _escalated([_judge_model()]), "pr-42-bbb2222")],
    )
    with (
        patch("sentinel.eval.runner.flush_traces"),
        patch("sentinel.eval.runner.push_score"),
        patch("sentinel.eval.judge.push_score"),
    ):
        run = await run_eval(
            branches_path=FIXTURE,
            items=[ok, conflict],
            settings=_settings(),
            reports_dir=tmp_path,
        )
    assert run.report.items_scored == 1
    assert run.report.judge_conflicts == 1
    assert run.report.scenarios_run == 1
    assert conflict.runs == []
    assert all(cs.item_count == 1 for cs in run.report.check_stats)


async def test_invalid_items_are_skipped(tmp_path: Path) -> None:
    items = [
        FakeItem("nope", {"branch": "nope/99", "incidents": []}),
        FakeItem("str", "not a dict"),
        _item("no-pr", "deployfail/01", None, None, []),
    ]
    with patch("sentinel.eval.runner.flush_traces"):
        run = await run_eval(
            branches_path=FIXTURE, items=items, settings=_settings(), reports_dir=tmp_path
        )
    assert set(run.invalid) == {"nope", "str", "no-pr"}
    assert run.results == []
    assert run.report.items_scored == 0
    assert run.json_path.exists()
    assert all(i.runs == [] for i in items)


async def test_push_failure_is_logged_not_raised(tmp_path: Path) -> None:
    item = BrokenItem("broken", {"branch": "pass/01", "incidents": []})
    with patch("sentinel.eval.runner.flush_traces"):
        run = await run_eval(
            branches_path=FIXTURE, items=[item], settings=_settings(), reports_dir=tmp_path
        )
    assert [r.pushed for r in run.results] == [False]
    assert run.report.items_scored == 1


async def test_flush_runs_even_when_the_catalog_is_missing(tmp_path: Path) -> None:
    with patch("sentinel.eval.runner.flush_traces") as flush, pytest.raises(EvalError):
        await run_eval(
            branches_path=tmp_path / "missing.yaml",
            items=[],
            settings=_settings(),
            reports_dir=tmp_path,
        )
    flush.assert_called_once()


# ── Score storing ─────────────────────────────────────────────────────────────


@contextmanager
def _quiet_langfuse() -> Iterator[tuple[MagicMock, MagicMock]]:
    with (
        patch("sentinel.eval.runner.flush_traces"),
        patch("sentinel.eval.runner.push_score") as ps,
        patch("sentinel.eval.judge.push_score") as pj,
    ):
        yield ps, pj


async def test_run_eval_stores_each_judged_score(tmp_path: Path) -> None:
    items = _dataset()
    store = FakeStore()
    with _quiet_langfuse():
        run = await run_eval(
            branches_path=FIXTURE,
            items=items,
            settings=_settings(),
            reports_dir=tmp_path,
            score_store=store,
        )
    assert [c[0] for c in store.calls] == [_primary_id(i) for i in items[1:]]
    assert [r.stored for r in run.results] == [False, True, True, True]
    for (_, stored_score), result in zip(store.calls, run.results[1:], strict=True):
        assert stored_score is result.judge_score


async def test_store_failures_are_logged_not_fatal(tmp_path: Path) -> None:
    items = _dataset()
    store = FakeStore(failing={_primary_id(items[2])}, missing={_primary_id(items[3])})
    with _quiet_langfuse(), patch("sentinel.eval.runner.logger") as log:
        run = await run_eval(
            branches_path=FIXTURE,
            items=items,
            settings=_settings(),
            reports_dir=tmp_path,
            score_store=store,
        )
    assert [r.stored for r in run.results] == [False, True, False, False]
    events = [c.args[0] for c in log.warning.call_args_list]
    assert "eval_score_store_failed" in events
    assert "eval_score_no_incident_row" in events
    assert run.json_path.exists()
    assert run.report.items_scored == 4


async def test_store_scores_false_skips_the_database(tmp_path: Path) -> None:
    store = FakeStore()
    with (
        _quiet_langfuse(),
        patch("sentinel.eval.runner.open_score_store", new_callable=AsyncMock) as open_store,
    ):
        run = await run_eval(
            branches_path=FIXTURE,
            items=_dataset(),
            settings=_settings(),
            reports_dir=tmp_path,
            store_scores=False,
            score_store=store,
        )
    open_store.assert_not_awaited()
    assert store.calls == []
    assert [r.stored for r in run.results] == [False] * 4


async def test_runner_opens_and_closes_its_own_store(tmp_path: Path) -> None:
    settings = _settings()
    with (
        _quiet_langfuse(),
        patch(
            "sentinel.eval.runner.open_score_store",
            new_callable=AsyncMock,
            return_value=FakeStore(),
        ) as open_store,
        patch("sentinel.eval.runner.close_score_store", new_callable=AsyncMock) as close_store,
    ):
        run = await run_eval(
            branches_path=FIXTURE, items=_dataset(), settings=settings, reports_dir=tmp_path
        )
    open_store.assert_awaited_once_with(settings)
    close_store.assert_awaited_once()
    assert [r.stored for r in run.results] == [False, True, True, True]


async def test_injected_store_is_never_closed(tmp_path: Path) -> None:
    with (
        _quiet_langfuse(),
        patch("sentinel.eval.runner.close_score_store", new_callable=AsyncMock) as close_store,
    ):
        await run_eval(
            branches_path=FIXTURE,
            items=_dataset(),
            settings=_settings(),
            reports_dir=tmp_path,
            score_store=FakeStore(),
        )
    close_store.assert_not_awaited()


async def test_no_database_scores_without_storing(tmp_path: Path) -> None:
    with (
        _quiet_langfuse(),
        patch("sentinel.eval.runner.close_score_store", new_callable=AsyncMock) as close_store,
    ):
        run = await run_eval(
            branches_path=FIXTURE, items=_dataset(), settings=_settings(), reports_dir=tmp_path
        )
    assert [r.stored for r in run.results] == [False] * 4
    close_store.assert_not_awaited()
    assert run.report.items_scored == 4


async def test_close_runs_even_when_scoring_raises(tmp_path: Path) -> None:
    with (
        _quiet_langfuse(),
        patch(
            "sentinel.eval.runner.open_score_store",
            new_callable=AsyncMock,
            return_value=FakeStore(),
        ),
        patch("sentinel.eval.runner.close_score_store", new_callable=AsyncMock) as close_store,
        patch("sentinel.eval.runner.build_report", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        await run_eval(
            branches_path=FIXTURE, items=_dataset(), settings=_settings(), reports_dir=tmp_path
        )
    close_store.assert_awaited_once()


async def test_judge_failed_item_is_stored_not_pushed_not_aggregated(tmp_path: Path) -> None:
    item = _dataset()[2]
    store = FakeStore()
    with (
        _quiet_langfuse() as (ps, pj),
        patch(
            "sentinel.eval.runner.make_judge_client",
            return_value=(_judge_model(), FailingClient()),
        ),
        patch("sentinel.eval.judge.asyncio.sleep", new_callable=AsyncMock),
    ):
        run = await run_eval(
            branches_path=FIXTURE,
            items=[item],
            settings=_settings(),
            reports_dir=tmp_path,
            score_store=store,
        )
    assert run.report.judge_failures == 1
    assert run.report.scenarios_run == 0
    assert run.report.items_scored == 1
    assert ps.call_count == 4
    pj.assert_not_called()
    judge_score = run.results[0].judge_score
    assert judge_score is not None
    assert judge_score.judge_failed is True
    assert store.calls[0][1].to_eval_score()["judge_failed"] is True


async def test_push_runs_off_the_event_loop(tmp_path: Path) -> None:
    ThreadItem.threads = []
    item = ThreadItem("pass-01", {"branch": "pass/01", "incidents": []})
    loop_thread = threading.get_ident()
    with _quiet_langfuse():
        run = await run_eval(
            branches_path=FIXTURE, items=[item], settings=_settings(), reports_dir=tmp_path
        )
    assert ThreadItem.threads
    assert ThreadItem.threads[0] != loop_thread
    assert run.results[0].pushed is True


# ── Dataset fetch ─────────────────────────────────────────────────────────────


def test_fetch_dataset_items_needs_langfuse() -> None:
    with pytest.raises(EvalError):
        fetch_dataset_items("x")

    a, b = object(), object()
    with (
        patch("sentinel.eval.runner.langfuse_enabled", return_value=True),
        patch("sentinel.eval.runner.get_client") as get_client,
    ):
        get_client.return_value.get_dataset.return_value.items = [a, b]
        assert fetch_dataset_items("x") == [a, b]
        get_client.return_value.get_dataset.assert_called_once_with("x")
        get_client.return_value.get_dataset.side_effect = RuntimeError("boom")
        with pytest.raises(EvalError):
            fetch_dataset_items("x")


async def test_run_eval_fetches_when_no_items_given(tmp_path: Path) -> None:
    with (
        patch("sentinel.eval.runner.fetch_dataset_items", return_value=_dataset()[:1]) as fetch,
        patch("sentinel.eval.runner.flush_traces"),
        patch("sentinel.eval.runner.push_score"),
    ):
        run = await run_eval(branches_path=FIXTURE, settings=_settings(), reports_dir=tmp_path)
    fetch.assert_called_once_with(DATASET_NAME)
    assert run.report.items_scored == 1
