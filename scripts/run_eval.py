"""Score finished scenario runs held in a LangFuse dataset against branches.yaml.

Usage::

    python3 scripts/run_eval.py --branches ../Sentinel-deployment/scenarios/branches.yaml
    python3 scripts/run_eval.py --branches <path> --dataset sentinel-scenario-runs --run-name R

This is a read-only scorer: run scenarios by merging their branch PRs; this only scores.

Results are written to:
  reports/eval_report.json  — machine-readable (for CI gates and dashboard)
  reports/eval_report.md    — human-readable Markdown summary

Exit status: 0 on success, 1 when no item was scored, 2 on a configuration error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure src/ is on sys.path when running the script directly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sentinel.eval.report import PASS_THRESHOLD  # noqa: E402
from sentinel.eval.runner import DATASET_NAME, run_eval  # noqa: E402
from sentinel.exceptions import EvalError, ProviderUnavailableError  # noqa: E402

_REPORTS_DIR = _PROJECT_ROOT / "reports"


def _score_bar(score: float, width: int = 20) -> str:
    """Render a text progress bar for a 0-1 score."""
    filled = int(round(score * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {score:.2f}/1.00"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score scenario runs from a LangFuse dataset.")
    parser.add_argument(
        "--branches",
        type=Path,
        required=True,
        help="path to scenarios/branches.yaml (the ground-truth catalog)",
    )
    parser.add_argument("--dataset", default=DATASET_NAME, help="LangFuse dataset name")
    parser.add_argument("--run-name", default=None, help="dataset-run name (default: timestamp)")
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    print("\nSentinel Eval Runner")
    print("=" * 40)
    try:
        run = await run_eval(
            branches_path=args.branches,
            dataset_name=args.dataset,
            run_name=args.run_name,
            reports_dir=_REPORTS_DIR,
        )
    except (EvalError, ProviderUnavailableError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    report = run.report
    print(f"  Dataset         : {run.dataset_name}")
    print(f"  Run name        : {run.run_name}")
    print(f"  Items scored    : {report.items_scored}")
    print(f"  Items invalid   : {len(run.invalid)}")
    print(f"  Judge conflicts : {report.judge_conflicts}")
    print(f"  Average score   : {_score_bar(report.avg_total_score)}")
    print(f"  Judge pass rate : {report.pass_rate * 100:.0f}%  (≥{PASS_THRESHOLD})")
    for cs in report.check_stats:
        print(f"  {str(cs.check):<15} : {cs.pass_rate * 100:.0f}%  ({cs.passed}/{cs.item_count})")
    print("  Reports written to:")
    print(f"    {run.json_path}")
    print(f"    {run.md_path}")

    if report.items_scored == 0:
        print("No items were scored. Check logs for errors.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
