# task-2 — asyncpg pool `infra/database.py`   ·   [category-3-sentinel-backend / phase-1-data-layer]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-1-data-layer` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §4.5, §5.1, §13.3 |
| **Depends on** | [[task-1-phase2-dependencies]] |
| **Referenced by** | [[task-1-episodic-memory]], [[task-2-semantic-memory]], [[task-5-deploy-checker]], [[task-5-app-lifespan-and-concurrency]] |

## Spec
New `src/sentinel/infra/database.py` — asyncpg pool (parallel to old `db.py`, which stays
until Phase 9 per §13.5 step 2).

**Files created:** `src/sentinel/infra/database.py`
- `async def create_pool(dsn: str, *, min_size=2, max_size=10) -> asyncpg.Pool` (bounds from config).
- Register the `pgvector` codec on each connection (`pgvector.asyncpg.register_vector`).
- `@asynccontextmanager acquire()` helper; module-level pool holder set at lifespan startup, closed at shutdown.
- `from __future__ import annotations`, full type hints, structured logging.

## Prerequisites
- [ ] task 1.1 deps. [ ] Local `pgvector/pgvector:pg16` for tests.

## Acceptance Criteria
- [ ] Pool creates/acquires/releases; vector codec registered; min/max honored from config.
- [ ] Clean async lifecycle (no leaked connections); errors wrapped in `MemoryError` per exception hierarchy.

## Tests
- **Unit/integration (`tests/test_infra/test_database.py`):** against local pgvector container — acquire a conn, `SELECT 1`, round-trip a `vector(3)` value.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `docker run pgvector/pgvector:pg16`; set DATABASE_URL local.
2. `pytest tests/test_infra/test_database.py -q` → green (pool + vector round-trip).

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally (uses local pgvector). Azure DB verify ⛔ B1 (not required for this task)._
