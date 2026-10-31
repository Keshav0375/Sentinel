"""Tests for eval/report.py — EvalReport generation, serialisation, and writing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentinel.eval.report import (
    PASS_THRESHOLD,
    CheckStats,
    DimensionStats,
    EvalReport,
    build_report,
    to_json_dict,
    to_markdown,
    write_json,
    write_markdown,
    write_reports,
)
from sentinel.models.eval_result import (
    DimensionScore,
    EvalDimension,
    GroundTruthCheck,
    GroundTruthResult,
    TrajectoryScore,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_score(
    scenario_id: str = "bad_deploy_01",
    total: float = 0.8,
    dim_score: float | None = None,
    judge_model: str = "test-judge",
    judge_conflict: bool = False,
    judge_failed: bool = False,
) -> TrajectoryScore:
    """Create a TrajectoryScore with uniform dimension scores."""
    s = dim_score if dim_score is not None else total
    return TrajectoryScore(
        incident_id=f"inc-{scenario_id}",
        scenario_id=scenario_id,
        dimension_scores=[
            DimensionScore(dimension=d, score=s, reasoning="ok") for d in EvalDimension
        ],
        total_score=total,
        judge_model=judge_model,
        judge_conflict=judge_conflict,
        judge_failed=judge_failed,
    )


def _two_scores() -> list[TrajectoryScore]:
    return [_make_score("bad_deploy_01", 0.8), _make_score("db_pool_01", 0.4)]


# ── build_report ──────────────────────────────────────────────────────────────


class TestBuildReport:
    def test_returns_eval_report(self) -> None:
        report = build_report(_two_scores())
        assert isinstance(report, EvalReport)

    def test_empty_scores_returns_zeroes(self) -> None:
        report = build_report([])
        assert report.avg_total_score == 0.0
        assert report.pass_rate == 0.0
        assert report.scenarios_run == 0

    def test_scenarios_run_count(self) -> None:
        report = build_report(_two_scores())
        assert report.scenarios_run == 2

    def test_avg_total_score_is_mean(self) -> None:
        report = build_report([_make_score("a", 0.4), _make_score("b", 0.8)])
        assert report.avg_total_score == pytest.approx(0.6)

    def test_pass_rate_default_threshold(self) -> None:
        # 0.8 passes, 0.4 fails → 50%
        report = build_report(_two_scores())
        assert report.pass_rate == pytest.approx(0.5)

    def test_pass_rate_custom_threshold(self) -> None:
        scores = [_make_score("a", 0.2), _make_score("b", 0.6), _make_score("c", 1.0)]
        report = build_report(scores, pass_threshold=0.4)
        assert report.pass_rate == pytest.approx(2 / 3, abs=0.01)

    def test_pass_threshold_stored(self) -> None:
        report = build_report(_two_scores(), pass_threshold=0.5)
        assert report.pass_threshold == pytest.approx(0.5)

    def test_judge_model_from_first_score(self) -> None:
        scores = [_make_score(judge_model="my-model"), _make_score(judge_model="other")]
        report = build_report(scores)
        assert report.judge_model == "my-model"

    def test_generated_at_is_iso_string(self) -> None:
        report = build_report(_two_scores())
        assert isinstance(report.generated_at, str)
        assert "T" in report.generated_at  # ISO format

    def test_scores_preserved(self) -> None:
        s = _two_scores()
        report = build_report(s)
        assert report.scores is s

    def test_five_dimension_stats(self) -> None:
        report = build_report(_two_scores())
        assert len(report.dimension_stats) == 5

    def test_dimension_stats_are_frozen(self) -> None:
        report = build_report(_two_scores())
        for ds in report.dimension_stats:
            assert isinstance(ds, DimensionStats)
            with pytest.raises(Exception):
                ds.avg_score = 99.0  # type: ignore[misc]

    def test_dimension_avg_computed_correctly(self) -> None:
        # Both scores have dim_score = total (uniform)
        scores = [_make_score("a", 0.6, dim_score=0.6), _make_score("b", 1.0, dim_score=1.0)]
        report = build_report(scores)
        for ds in report.dimension_stats:
            assert ds.avg_score == pytest.approx(0.8)

    def test_dimension_min_max(self) -> None:
        scores = [_make_score("a", 0.2, dim_score=0.2), _make_score("b", 1.0, dim_score=1.0)]
        report = build_report(scores)
        for ds in report.dimension_stats:
            assert ds.min_score == pytest.approx(0.2)
            assert ds.max_score == pytest.approx(1.0)


# ── to_json_dict ──────────────────────────────────────────────────────────────


class TestToJsonDict:
    def test_returns_dict(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        assert isinstance(d, dict)

    def test_has_generated_at(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        assert "generated_at" in d

    def test_has_summary_block(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        assert "summary" in d
        assert "scenarios_run" in d["summary"]
        assert "avg_total_score" in d["summary"]
        assert "pass_rate" in d["summary"]
        assert "pass_threshold" in d["summary"]

    def test_has_results_list(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        assert "results" in d
        assert isinstance(d["results"], list)
        assert len(d["results"]) == 2

    def test_results_have_scenario_id(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        for r in d["results"]:
            assert "scenario_id" in r

    def test_results_have_dimension_scores(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        for r in d["results"]:
            assert "dimension_scores" in r
            assert len(r["dimension_scores"]) == 5

    def test_results_have_total_score(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        for r in d["results"]:
            assert "total_score" in r

    def test_has_dimension_stats(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        assert "dimension_stats" in d
        assert len(d["dimension_stats"]) == 5

    def test_json_serialisable(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        text = json.dumps(d)
        assert isinstance(text, str)

    def test_empty_report_serialisable(self) -> None:
        d = to_json_dict(build_report([]))
        assert d["results"] == []


# ── to_markdown ───────────────────────────────────────────────────────────────


class TestToMarkdown:
    def test_returns_string(self) -> None:
        md = to_markdown(build_report(_two_scores()))
        assert isinstance(md, str)

    def test_contains_title(self) -> None:
        md = to_markdown(build_report(_two_scores()))
        assert "Sentinel Eval Report" in md

    def test_contains_summary_section(self) -> None:
        md = to_markdown(build_report(_two_scores()))
        assert "## Summary" in md

    def test_contains_dimension_averages_section(self) -> None:
        md = to_markdown(build_report(_two_scores()))
        assert "## Dimension Averages" in md

    def test_contains_per_scenario_breakdown(self) -> None:
        md = to_markdown(build_report(_two_scores()))
        assert "Per-Scenario Breakdown" in md

    def test_all_five_dimensions_listed(self) -> None:
        md = to_markdown(build_report(_two_scores()))
        for dim in EvalDimension:
            assert str(dim) in md

    def test_scenario_ids_in_table(self) -> None:
        md = to_markdown(build_report(_two_scores()))
        assert "bad_deploy_01" in md
        assert "db_pool_01" in md

    def test_pass_fail_markers(self) -> None:
        md = to_markdown(build_report(_two_scores()))
        assert "PASS" in md
        assert "FAIL" in md

    def test_judge_model_shown(self) -> None:
        md = to_markdown(build_report([_make_score(judge_model="my-judge")]))
        assert "my-judge" in md


# ── write_json / write_markdown / write_reports ───────────────────────────────


class TestWriteFunctions:
    def test_write_json_creates_file(self, tmp_path: Path) -> None:
        report = build_report(_two_scores())
        path = tmp_path / "eval.json"
        write_json(report, path)
        assert path.exists()

    def test_write_json_valid_json(self, tmp_path: Path) -> None:
        report = build_report(_two_scores())
        path = tmp_path / "eval.json"
        write_json(report, path)
        data = json.loads(path.read_text())
        assert "results" in data

    def test_write_json_creates_parent_dirs(self, tmp_path: Path) -> None:
        report = build_report(_two_scores())
        path = tmp_path / "sub" / "dir" / "eval.json"
        write_json(report, path)
        assert path.exists()

    def test_write_markdown_creates_file(self, tmp_path: Path) -> None:
        report = build_report(_two_scores())
        path = tmp_path / "eval.md"
        write_markdown(report, path)
        assert path.exists()

    def test_write_markdown_contains_title(self, tmp_path: Path) -> None:
        report = build_report(_two_scores())
        path = tmp_path / "eval.md"
        write_markdown(report, path)
        assert "Sentinel Eval Report" in path.read_text()

    def test_write_reports_returns_both_paths(self, tmp_path: Path) -> None:
        report = build_report(_two_scores())
        json_path, md_path = write_reports(report, reports_dir=tmp_path)
        assert json_path.exists()
        assert md_path.exists()

    def test_write_reports_default_filenames(self, tmp_path: Path) -> None:
        report = build_report(_two_scores())
        json_path, md_path = write_reports(report, reports_dir=tmp_path)
        assert json_path.name == "eval_report.json"
        assert md_path.name == "eval_report.md"

    def test_write_reports_custom_filenames(self, tmp_path: Path) -> None:
        report = build_report(_two_scores())
        json_path, md_path = write_reports(
            report,
            reports_dir=tmp_path,
            json_filename="custom.json",
            md_filename="custom.md",
        )
        assert json_path.name == "custom.json"
        assert md_path.name == "custom.md"

    def test_write_reports_json_has_results_key(self, tmp_path: Path) -> None:
        report = build_report(_two_scores())
        json_path, _ = write_reports(report, reports_dir=tmp_path)
        data = json.loads(json_path.read_text())
        assert "results" in data
        assert len(data["results"]) == 2


# ── PASS_THRESHOLD constant ───────────────────────────────────────────────────


class TestPassThreshold:
    def test_default_threshold_is_0_6(self) -> None:
        assert PASS_THRESHOLD == pytest.approx(0.6)

    def test_threshold_used_in_build_report(self) -> None:
        score_below = _make_score("a", 0.58)
        score_above = _make_score("b", 0.6)
        report = build_report([score_below, score_above])
        assert report.pass_rate == pytest.approx(0.5)


# ── judge_conflict exclusion (decision 2026-10-18) ────────────────────────────


class TestJudgeConflictExcluded:
    def test_conflicts_are_kept_but_not_aggregated(self) -> None:
        scores = [
            _make_score("bad_deploy_01", 0.8),
            _make_score("db_pool_01", 0.0, judge_conflict=True),
        ]
        report = build_report(scores)
        assert report.scenarios_run == 1
        assert report.judge_conflicts == 1
        assert report.avg_total_score == pytest.approx(0.8)
        assert report.pass_rate == pytest.approx(1.0)
        assert all(ds.scenario_count == 1 for ds in report.dimension_stats)
        assert len(report.scores) == 2
        assert to_json_dict(report)["summary"]["judge_conflicts"] == 1
        assert "JUDGE_CONFLICT" in to_markdown(report)

    def test_only_conflicts_reports_zero_scenarios(self) -> None:
        report = build_report([_make_score(total=1.0, judge_conflict=True)])
        assert report.scenarios_run == 0
        assert report.avg_total_score == 0.0
        assert report.judge_conflicts == 1


class TestJudgeFailedExcluded:
    def test_failures_are_kept_but_not_aggregated(self) -> None:
        scores = [
            _make_score("a", 0.8),
            _make_score("b", 0.0, judge_failed=True),
        ]
        report = build_report(scores)
        assert report.scenarios_run == 1
        assert report.judge_failures == 1
        assert report.judge_conflicts == 0
        assert report.avg_total_score == pytest.approx(0.8)
        assert report.pass_rate == pytest.approx(1.0)
        assert all(ds.scenario_count == 1 for ds in report.dimension_stats)
        assert len(report.scores) == 2
        assert to_json_dict(report)["summary"]["judge_failures"] == 1
        markdown = to_markdown(report)
        assert "JUDGE_FAILED" in markdown
        assert "Judge failures (excluded) | 1" in markdown

    def test_failed_ground_truth_item_still_counts(self) -> None:
        gt = GroundTruthResult(
            item_id="x",
            branch="runtime/01",
            case="iii",
            checks=dict.fromkeys(GroundTruthCheck, 1.0),
        )
        report = build_report([_make_score(total=0.0, judge_failed=True)], ground_truth=[gt])
        assert report.items_scored == 1
        assert all(cs.item_count == 1 for cs in report.check_stats)
        assert report.scenarios_run == 0

    def test_only_failures_reports_zero_scenarios(self) -> None:
        report = build_report([_make_score(total=1.0, judge_failed=True)])
        assert report.scenarios_run == 0
        assert report.avg_total_score == 0.0
        assert report.judge_failures == 1


class TestScaleDisplay:
    def test_markdown_shows_0_1_scale(self) -> None:
        assert "/ 1.00" in to_markdown(build_report(_two_scores()))


# ── Ground-truth checks ───────────────────────────────────────────────────────


def _gt(item_id: str, value: float, *, conflict: bool = False) -> GroundTruthResult:
    return GroundTruthResult(
        item_id=item_id,
        branch="pass/01",
        case="i",
        checks=dict.fromkeys(GroundTruthCheck, value),
        judge_conflict=conflict,
    )


class TestGroundTruth:
    def test_check_stats_one_per_check_and_conflicts_excluded(self) -> None:
        gt = [_gt("a", 1.0), _gt("b", 0.0, conflict=True), _gt("c", 0.0)]
        report = build_report([], ground_truth=gt)
        assert report.items_scored == 2
        assert [cs.check for cs in report.check_stats] == list(GroundTruthCheck)
        assert all(isinstance(cs, CheckStats) for cs in report.check_stats)
        assert all(cs.pass_rate == 0.5 and cs.item_count == 2 for cs in report.check_stats)
        assert len(report.ground_truth) == 3

    def test_no_ground_truth_gives_zero_stats(self) -> None:
        report = build_report([])
        assert len(report.check_stats) == 4
        assert all(cs.item_count == 0 and cs.pass_rate == 0.0 for cs in report.check_stats)
        assert report.items_scored == 0
        assert report.dataset_name is None

    def test_json_has_ground_truth_and_run_fields(self) -> None:
        report = build_report(
            [_make_score()], ground_truth=[_gt("a", 1.0)], dataset_name="d", run_name="r"
        )
        data = to_json_dict(report)
        assert data["summary"]["dataset"] == "d"
        assert data["summary"]["run_name"] == "r"
        assert data["summary"]["items_scored"] == 1
        assert len(data["ground_truth"]["checks"]) == 4
        assert data["ground_truth"]["items"][0]["item_id"] == "a"
        assert data["ground_truth"]["items"][0]["incident_id"] is None
        assert data["ground_truth"]["items"][0]["judge_failed"] is False
        assert data["ground_truth"]["items"][0]["created_at"] is None
        json.dumps(data)

    def test_json_serialises_created_at(self) -> None:
        gt = GroundTruthResult(
            item_id="a",
            branch="deployfail/05",
            case="ii",
            checks=dict.fromkeys(GroundTruthCheck, 1.0),
            incident_id="inc-1",
            created_at=datetime(2026, 10, 27, 12, tzinfo=UTC),
        )
        data = to_json_dict(build_report([], ground_truth=[gt]))
        item = data["ground_truth"]["items"][0]
        assert item["created_at"] == "2026-10-27T12:00:00Z"
        assert item["incident_id"] == "inc-1"
        json.dumps(data)

    def test_markdown_has_ground_truth_section(self) -> None:
        with_gt = to_markdown(build_report([], ground_truth=[_gt("a", 1.0)]))
        assert "## Ground-Truth Checks" in with_gt
        assert "| revert_target | 100% | 1 | 1 |" in with_gt
        assert "## Ground-Truth Checks" not in to_markdown(build_report([]))
