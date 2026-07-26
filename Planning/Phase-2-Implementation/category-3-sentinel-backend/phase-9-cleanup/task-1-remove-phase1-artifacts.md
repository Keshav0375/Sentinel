# task-1 — Remove Phase-1 artifacts + deps   ·   [category-3-sentinel-backend / phase-9-cleanup]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-9-cleanup` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §13.1 (delete), §13.4 (deps), §13.5 step 8 |
| **Depends on** | ALL prior backend phases `verified` (Phase 2 tools proven before deleting Phase 1) |
| **Referenced by** | [[task-2-end-to-end-integration]] |

## Spec
Delete the Phase-1 synthetic-data machinery now that Phase-2 is proven (§13.5 step 8 is
intentionally late).

**Files deleted (§13.1):** `data/` (all), `src/sentinel/generator/` (all 4), `src/sentinel/infra/db.py`, `scripts/run_scenario.py`, `scripts/run_eval.py`, `scripts/demo.py`, `docker-compose.yml`.
**Deps removed (§13.4):** `aiosqlite`; `numpy` (keep only if transitively required by sentence-transformers).
**Files modified:** `pyproject.toml` (drop deps), `Dockerfile` (already no `data/` from 7.1), any lingering imports.

## Prerequisites
- [ ] Every prior backend phase `verified`. [ ] grep confirms no runtime code imports `data/`, `generator/`, `db.py`, or removed scripts.

## Acceptance Criteria
- [ ] All listed paths removed; app boots; full suite green; no dangling imports.
- [ ] `aiosqlite` gone; `numpy` gone or transitive-only; lockfile regenerated.

## Tests
- **Boot check + full suite:** app starts; `pytest -q` green post-removal.
- **grep:** no references to deleted modules remain.
- **Quality gate:** `--repo backend` (pip-audit clean of removed deps).

## How to Verify (phase gate)
1. `grep -r "sentinel.generator\|infra.db\|data/scenarios" src/ tests/` → no hits.
2. App boots; `pytest -q` green; `pip show aiosqlite` → not installed.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Gated on all prior phases verified (do NOT delete Phase-1 until Phase-2 proven)._
