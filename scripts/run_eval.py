"""Run the full eval suite across all (or selected) scenarios.

Usage::

    python scripts/run_eval.py
    python scripts/run_eval.py --scenarios bad_deploy_01 db_pool_01
    python scripts/run_eval.py --judge-model llama-3.3-70b-versatile
    python scripts/run_eval.py --help

Results are written to:
  reports/eval_report.json  — machine-readable (for CI gates and dashboard)
  reports/eval_report.md    — human-readable Markdown summary

Run from the project root so that ``src/`` and ``data/`` are importable.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure src/ is on sys.path when running the script directly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sentinel.eval.report import build_report, write_reports  # noqa: E402
from sentinel.eval.runner import (  # noqa: E402
    EvalRunResult,
    run_eval,
    scores_from_results,
    summary_stats,
)

_REPORTS_DIR = _PROJECT_ROOT / "reports"
_PASS_THRESHOLD = 3.0


def _score_bar(score: float, width: int = 20) -> str:
    """Render a text progress bar for a 0-5 score."""
    filled = int(round(score / 5.0 * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {score:.2f}/5.00"


def _print_summary(results: list[EvalRunResult]) -> None:
    """Print a human-readable summary table to stdout."""
    sep = "=" * 72

    print(f"\n{sep}")
    print("  Sentinel Eval Results")
    print(sep)

    stats = summary_stats(results)
    print(f"  Scenarios run : {len(results)}")
    print(f"  Average score : {_score_bar(stats['avg_total'])}")
    print(f"  Pass rate     : {stats['pass_rate'] * 100:.0f}%  (≥{_PASS_THRESHOLD}/5)")
    print(f"  Failures      : {stats['failure_rate'] * 100:.0f}%  (pipeline error)")
    print()

    # Per-scenario rows
    print(f"  {'Scenario':<32} {'Total':>6}  {'Pass':>4}  Notes")
    print(f"  {'-' * 30:<32} {'-' * 6:>6}  {'-' * 4:>4}  {'-' * 14}")
    for r in results:
        passed = "PASS" if r.score.total_score >= _PASS_THRESHOLD else "FAIL"
        note = ""
        if r.pipeline_error:
            note = "pipeline error"
        elif r.trajectory_missing:
            note = "no trajectory"
        print(
            f"  {r.scenario_id:<32} {r.score.total_score:>6.2f}  {passed:>4}  {note}"
        )

    print(f"\n{sep}\n")


def _parse_args() -> tuple[list[str] | None, str | None]:
    """Return (scenario_ids, judge_model) from sys.argv."""
    args = sys.argv[1:]

    if "-h" in args or "--help" in args:
        print(
            "Usage:\n"
            "  python scripts/run_eval.py\n"
            "  python scripts/run_eval.py --scenarios bad_deploy_01 db_pool_01\n"
            "  python scripts/run_eval.py --judge-model llama-3.3-70b-versatile\n"
        )
        sys.exit(0)

    scenario_ids: list[str] | None = None
    judge_model: str | None = None

    i = 0
    while i < len(args):
        if args[i] == "--scenarios":
            scenario_ids = []
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                scenario_ids.append(args[i])
                i += 1
        elif args[i] == "--judge-model" and i + 1 < len(args):
            judge_model = args[i + 1]
            i += 2
        else:
            i += 1

    return scenario_ids, judge_model


async def main() -> None:
    scenario_ids, judge_model = _parse_args()

    print("\nSentinel Eval Runner")
    print("=" * 40)
    if scenario_ids:
        print(f"  Scenarios : {', '.join(scenario_ids)}")
    else:
        print("  Scenarios : all")
    if judge_model:
        print(f"  Judge     : {judge_model}")
    print()

    results = await run_eval(
        scenario_ids=scenario_ids,
        judge_model=judge_model,
    )

    if not results:
        print("No scenarios were evaluated. Check logs for errors.")
        sys.exit(1)

    _print_summary(results)

    report = build_report(scores_from_results(results))
    json_path, md_path = write_reports(report, reports_dir=_REPORTS_DIR)
    print("  Reports written to:")
    print(f"    {json_path}")
    print(f"    {md_path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
