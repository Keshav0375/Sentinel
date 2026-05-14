"""Eval report API — GET /api/eval-report.

Returns the latest eval report JSON from ``reports/eval_report.json`` so the
eval results dashboard can render per-scenario scores and dimension averages.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

_DEFAULT_REPORT_PATH = Path("reports/eval_report.json")


def make_eval_report_router(
    report_path: Path = _DEFAULT_REPORT_PATH,
) -> APIRouter:
    """Create the eval report router.

    Args:
        report_path: Path to the eval report JSON file. Defaults to
            ``reports/eval_report.json``.

    Returns:
        ``APIRouter`` with ``GET /api/eval-report`` registered.
    """
    router = APIRouter()

    @router.get("/api/eval-report")
    async def get_eval_report() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Return the latest eval report.

        Reads from disk on every request so the page always shows the most
        recent run without server restart.

        Raises:
            HTTPException: 404 if the report file does not exist yet.
        """
        if not report_path.exists():
            raise HTTPException(
                status_code=404,
                detail="No eval report found. Run the eval suite first: python scripts/run_eval.py",
            )
        text = report_path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(text)
        return data

    return router
