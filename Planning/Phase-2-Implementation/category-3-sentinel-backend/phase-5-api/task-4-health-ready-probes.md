# task-4 — `/health` + `/ready` probes   ·   [category-3-sentinel-backend / phase-5-api]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-5-api` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §3.5, §9.1 (FAKE_LLM skips live check), §8.1 (probes) |
| **Depends on** | [[task-2-asyncpg-database-pool]] |
| **Referenced by** | [[task-2-k8s-manifests]] (probes), [[task-3-composite-actions]] (backend-up poll), CI |

## Spec
Liveness + readiness. Open (no token) so K8s + backend-up can probe.

**Files created/modified:** `src/sentinel/api/health.py`
- `GET /health` → 200 `{"status":"ok"}` (liveness, always if process up).
- `GET /ready` → 200 `{"status":"ready"}` when DB connected + models loaded; else 503 `{"status":"not_ready"}`. Under `SENTINEL_FAKE_LLM=1`, skip the live-provider check (§9.1).

## Prerequisites
- [ ] task 1.2 pool.

## Acceptance Criteria
- [ ] `/health` 200 always; `/ready` reflects DB connectivity (503 when pool down); no token required.
- [ ] FAKE_LLM mode makes `/ready` deterministic for CI.

## Tests
- **Integration (`tests/test_api/test_health.py`):** `/health` 200; `/ready` 200 with DB up, 503 with DB down (pool closed).
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_api/test_health.py -q` green.
2. `curl .../health` → ok; `curl .../ready` → ready with DB up.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally._
