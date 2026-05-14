"""Tests for eval/report.py — EvalReport generation, serialisation, and writing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.eval.report import (
    PASS_THRESHOLD,
    DimensionStats,
    EvalReport,
    build_report,
    to_json_dict,
    to_markdown,
    write_json,
    write_markdown,
    write_reports,
)
from sentinel.models.eval_result import DimensionScore, EvalDimension, TrajectoryScore

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_score(
    scenario_id: str = "bad_deploy_01",
    total: float = 4.0,
    dim_score: float | None = None,
    judge_model: str = "test-judge",
) -> TrajectoryScore:
    """Create a TrajectoryScore with uniform dimension scores."""
    s = dim_score if dim_score is not None else total
    return TrajectoryScore(
        incident_id=f"inc-{scenario_id}",
        scenario_id=scenario_id,
        dimension_scores=[
            DimensionScore(dimension=d, score=s, reasoning="ok")
            for d in EvalDimension
        ],
        total_score=total,
        judge_model=judge_model,
    )


def _two_scores() -> list[TrajectoryScore]:
    return [_make_score("bad_deploy_01", 4.0), _make_score("db_pool_01", 2.0)]


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
        report = build_report([_make_score("a", 2.0), _make_score("b", 4.0)])
        assert report.avg_total_score == pytest.approx(3.0)

    def test_pass_rate_default_threshold(self) -> None:
        # 4.0 passes, 2.0 fails → 50%
        report = build_report(_two_scores())
        assert report.pass_rate == pytest.approx(0.5)

    def test_pass_rate_custom_threshold(self) -> None:
        scores = [_make_score("a", 1.0), _make_score("b", 3.0), _make_score("c", 5.0)]
        report = build_report(scores, pass_threshold=2.0)
        assert report.pass_rate == pytest.approx(2 / 3, abs=0.01)

    def test_pass_threshold_stored(self) -> None:
        report = build_report(_two_scores(), pass_threshold=2.5)
        assert report.pass_threshold == pytest.approx(2.5)

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

    def test_six_dimension_stats(self) -> None:
        report = build_report(_two_scores())
        assert len(report.dimension_stats) == 6

    def test_dimension_stats_are_frozen(self) -> None:
        report = build_report(_two_scores())
        for ds in report.dimension_stats:
            assert isinstance(ds, DimensionStats)
            with pytest.raises(Exception):
                ds.avg_score = 99.0  # type: ignore[misc]

    def test_dimension_avg_computed_correctly(self) -> None:
        # Both scores have dim_score = total (uniform)
        scores = [_make_score("a", 3.0, dim_score=3.0), _make_score("b", 5.0, dim_score=5.0)]
        report = build_report(scores)
        for ds in report.dimension_stats:
            assert ds.avg_score == pytest.approx(4.0)

    def test_dimension_min_max(self) -> None:
        scores = [_make_score("a", 1.0, dim_score=1.0), _make_score("b", 5.0, dim_score=5.0)]
        report = build_report(scores)
        for ds in report.dimension_stats:
            assert ds.min_score == pytest.approx(1.0)
            assert ds.max_score == pytest.approx(5.0)


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
            assert len(r["dimension_scores"]) == 6

    def test_results_have_total_score(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        for r in d["results"]:
            assert "total_score" in r

    def test_has_dimension_stats(self) -> None:
        d = to_json_dict(build_report(_two_scores()))
        assert "dimension_stats" in d
        assert len(d["dimension_stats"]) == 6

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

    def test_all_six_dimensions_listed(self) -> None:
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
    def test_default_threshold_is_3(self) -> None:
        assert PASS_THRESHOLD == pytest.approx(3.0)

    def test_threshold_used_in_build_report(self) -> None:
        score_below = _make_score("a", 2.9)
        score_above = _make_score("b", 3.0)
        report = build_report([score_below, score_above])
        assert report.pass_rate == pytest.approx(0.5)
