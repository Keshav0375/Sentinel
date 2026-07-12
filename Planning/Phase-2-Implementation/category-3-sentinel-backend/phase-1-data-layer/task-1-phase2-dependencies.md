# task-1 — Add Phase-2 deps (asyncpg, pgvector, alembic, langfuse)   ·   [category-3-sentinel-backend / phase-1-data-layer]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-1-data-layer` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §11, §13.5 step 1 |
| **Depends on** | — |
| **Referenced by** | all backend phases |

## Spec
Add Phase-2 dependencies; do NOT remove Phase-1 deps yet (§13.5 keeps Phase 1 working until
Phase 2 is proven — removal is Phase 9).

**Files modified:** `pyproject.toml` — add `asyncpg`, `pgvector`, `alembic`, `langfuse`,
ensure `httpx`. Keep `aiosqlite`/`numpy` for now. Update lockfile.

## Prerequisites
- [ ] Python 3.12 env. [ ] Package manager per repo (Poetry or pip — match existing).

## Acceptance Criteria
- [ ] New deps resolve and install cleanly; app still imports and boots (Phase-1 intact).
- [ ] Lockfile regenerated (per CLAUDE.md post-implementation check).

## Tests
- **Unit:** existing suite still green. **Boot check:** `sentinel serve` / `uvicorn sentinel.main:app` starts (CLAUDE.md non-negotiable startup check).
- **Quality gate:** `--repo backend` (ruff · pyright · pytest · pip-audit).

## How to Verify (phase gate)
1. `pip install -e ".[dev]"` (or `poetry install`) succeeds.
2. App boots without import errors; `pytest -q` green.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none — local._
