"""Eval report generation — JSON and Markdown from TrajectoryScore lists.

Usage::

    from sentinel.eval.report import build_report, write_reports

    report = build_report(scores)
    json_path, md_path = write_reports(report)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sentinel.models.eval_result import EvalDimension, TrajectoryScore

__all__ = [
    "PASS_THRESHOLD",
    "DimensionStats",
    "EvalReport",
    "build_report",
    "to_json_dict",
    "to_markdown",
    "write_json",
    "write_markdown",
    "write_reports",
]

PASS_THRESHOLD: float = 3.0
_SCORE_MAX: float = 5.0


@dataclass(frozen=True)
class DimensionStats:
    """Aggregate statistics for one eval dimension across all scenarios."""

    dimension: EvalDimension
    avg_score: float
    min_score: float
    max_score: float
    scenario_count: int


@dataclass(frozen=True)
class EvalReport:
    """Complete eval report computed from a list of TrajectoryScores.

    Produced by ``build_report()``. Serialised to JSON by ``to_json_dict()``
    and to Markdown by ``to_markdown()``.
    """

    scores: list[TrajectoryScore]
    dimension_stats: list[DimensionStats]
    avg_total_score: float
    pass_rate: float
    pass_threshold: float
    scenarios_run: int
    generated_at: str
    judge_model: str


# ── Builder ───────────────────────────────────────────────────────────────────


def build_report(
    scores: list[TrajectoryScore],
    pass_threshold: float = PASS_THRESHOLD,
) -> EvalReport:
    """Compute an EvalReport from a list of scored trajectories.

    Args:
        scores: One ``TrajectoryScore`` per scenario that was evaluated.
        pass_threshold: Minimum ``total_score`` to count as a pass.
            Defaults to 3.0 (60% of the 0-5 scale).

    Returns:
        ``EvalReport`` with per-dimension stats, pass rate, and metadata.
    """
    n = len(scores)
    generated_at = datetime.now(UTC).isoformat()
    judge_model = scores[0].judge_model if scores else "unknown"

    if n == 0:
        return EvalReport(
            scores=[],
            dimension_stats=_empty_dimension_stats(),
            avg_total_score=0.0,
            pass_rate=0.0,
            pass_threshold=pass_threshold,
            scenarios_run=0,
            generated_at=generated_at,
            judge_model=judge_model,
        )

    avg_total = sum(s.total_score for s in scores) / n
    pass_rate = sum(1 for s in scores if s.total_score >= pass_threshold) / n

    dimension_stats = _compute_dimension_stats(scores)

    return EvalReport(
        scores=scores,
        dimension_stats=dimension_stats,
        avg_total_score=round(avg_total, 4),
        pass_rate=round(pass_rate, 4),
        pass_threshold=pass_threshold,
        scenarios_run=n,
        generated_at=generated_at,
        judge_model=judge_model,
    )


def _empty_dimension_stats() -> list[DimensionStats]:
    return [
        DimensionStats(
            dimension=dim,
            avg_score=0.0,
            min_score=0.0,
            max_score=0.0,
            scenario_count=0,
        )
        for dim in EvalDimension
    ]


def _compute_dimension_stats(scores: list[TrajectoryScore]) -> list[DimensionStats]:
    """Compute per-dimension aggregate stats across all scores."""
    sums: dict[EvalDimension, float] = {d: 0.0 for d in EvalDimension}
    mins: dict[EvalDimension, float] = {d: _SCORE_MAX for d in EvalDimension}
    maxs: dict[EvalDimension, float] = {d: 0.0 for d in EvalDimension}
    counts: dict[EvalDimension, int] = {d: 0 for d in EvalDimension}

    for score in scores:
        for ds in score.dimension_scores:
            dim = ds.dimension
            sums[dim] += ds.score
            mins[dim] = min(mins[dim], ds.score)
            maxs[dim] = max(maxs[dim], ds.score)
            counts[dim] += 1

    return [
        DimensionStats(
            dimension=dim,
            avg_score=round(sums[dim] / counts[dim], 4) if counts[dim] > 0 else 0.0,
            min_score=mins[dim] if counts[dim] > 0 else 0.0,
            max_score=maxs[dim] if counts[dim] > 0 else 0.0,
            scenario_count=counts[dim],
        )
        for dim in EvalDimension
    ]


# ── Serialisers ───────────────────────────────────────────────────────────────


def to_json_dict(report: EvalReport) -> dict[str, Any]:
    """Convert an EvalReport to the JSON dict served by GET /api/eval-report.

    The output shape is:
    ``{"generated_at": ..., "summary": {...}, "dimension_stats": [...],
       "results": [TrajectoryScore.model_dump(), ...]}``

    The ``results`` key is the array the eval dashboard (GET /eval) expects.
    """
    return {
        "generated_at": report.generated_at,
        "summary": {
            "scenarios_run": report.scenarios_run,
            "avg_total_score": report.avg_total_score,
            "pass_rate": report.pass_rate,
            "pass_threshold": report.pass_threshold,
            "judge_model": report.judge_model,
        },
        "dimension_stats": [
            {
                "dimension": str(ds.dimension),
                "avg_score": ds.avg_score,
                "min_score": ds.min_score,
                "max_score": ds.max_score,
                "scenario_count": ds.scenario_count,
            }
            for ds in report.dimension_stats
        ],
        "results": [s.model_dump() for s in report.scores],
    }


def to_markdown(report: EvalReport) -> str:
    """Convert an EvalReport to a human-readable Markdown string.

    Produces a document with three sections:
    - Summary table (scenarios, avg score, pass rate)
    - Dimension averages table (avg, min, max per dimension)
    - Per-scenario breakdown table (one row per scored scenario)
    """
    lines: list[str] = []

    lines.append("# Sentinel Eval Report\n")
    lines.append(f"Generated: {report.generated_at}  ")
    lines.append(f"Judge model: `{report.judge_model}`\n")

    # ── Summary ────────────────────────────────────────────────────────────────
    lines.append("## Summary\n")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Scenarios evaluated | {report.scenarios_run} |")
    lines.append(f"| Average total score | {report.avg_total_score:.2f} / 5.00 |")
    lines.append(f"| Pass rate (≥{report.pass_threshold}) | {report.pass_rate * 100:.0f}% |")
    pf = "PASS" if report.avg_total_score >= report.pass_threshold else "FAIL"
    lines.append(f"| Overall | **{pf}** |\n")

    # ── Dimension averages ─────────────────────────────────────────────────────
    lines.append("## Dimension Averages\n")
    lines.append("| Dimension | Avg | Min | Max |")
    lines.append("|-----------|-----|-----|-----|")
    for ds in report.dimension_stats:
        lines.append(
            f"| {ds.dimension} | {ds.avg_score:.2f} | {ds.min_score:.2f} | {ds.max_score:.2f} |"
        )

    # ── Per-scenario breakdown ─────────────────────────────────────────────────
    lines.append("\n## Per-Scenario Breakdown\n")
    dim_labels = [str(d) for d in EvalDimension]
    headers = ["Scenario", "Total", "Pass"] + dim_labels
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")

    for score in report.scores:
        by_dim = {str(ds.dimension): ds.score for ds in score.dimension_scores}
        passed = "PASS" if score.total_score >= report.pass_threshold else "FAIL"
        row = [score.scenario_id, f"{score.total_score:.2f}", passed]
        row += [f"{by_dim.get(d, 0.0):.1f}" for d in dim_labels]
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines) + "\n"


# ── Writers ───────────────────────────────────────────────────────────────────


def write_json(report: EvalReport, path: Path) -> None:
    """Write the JSON report to ``path``.

    Creates parent directories if needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_json_dict(report), indent=2),
        encoding="utf-8",
    )


def write_markdown(report: EvalReport, path: Path) -> None:
    """Write the Markdown report to ``path``.

    Creates parent directories if needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(report), encoding="utf-8")


def write_reports(
    report: EvalReport,
    *,
    reports_dir: Path = Path("reports"),
    json_filename: str = "eval_report.json",
    md_filename: str = "eval_report.md",
) -> tuple[Path, Path]:
    """Write both JSON and Markdown reports to ``reports_dir``.

    Args:
        report: The eval report to serialise.
        reports_dir: Directory to write into. Created if it does not exist.
        json_filename: JSON output filename. Defaults to ``eval_report.json``.
        md_filename: Markdown output filename. Defaults to ``eval_report.md``.

    Returns:
        ``(json_path, md_path)`` — the paths of the written files.
    """
    json_path = reports_dir / json_filename
    md_path = reports_dir / md_filename
    write_json(report, json_path)
    write_markdown(report, md_path)
    return json_path, md_path
