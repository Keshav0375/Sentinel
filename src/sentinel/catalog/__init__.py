"""The scenario catalog: the vendored copy of Sentinel-deployment ``scenarios/branches.yaml``.

ONE copy, shipped in the package (decision 2026-10-25): ``GET /api/scenarios`` serves it and the
eval tests score against it. Refresh it by copying the deployment repo's file unchanged.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final

from sentinel.eval.runner import BranchTruth, load_branches

CATALOG_PATH: Final[Path] = Path(__file__).resolve().parent / "branches.yaml"


@lru_cache(maxsize=1)
def load_catalog() -> tuple[BranchTruth, ...]:
    """Every catalog entry, in file order (read once: the file never changes at runtime)."""
    return tuple(load_branches(CATALOG_PATH).values())
