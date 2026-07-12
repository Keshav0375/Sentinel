# task-5 — `main.py` lifespan + concurrency + token auth   ·   [category-3-sentinel-backend / phase-5-api]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-5-api` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §1.1 (routes), §3 (auth), §4.5 (concurrency) |
| **Depends on** | [[task-2-asyncpg-database-pool]], [[task-4-langfuse-tracing]], [[task-1-webhook-receiver]] |
| **Referenced by** | all API routes, [[task-1-ci-validation]] (boots the app) |

## Spec
Wire the FastAPI app: lifespan (open asyncpg pool + init LangFuse at startup, close at
shutdown), the `X-Sentinel-Token` auth dependency on all non-health routes, router
registration, and structlog config.

**Files modified:** `src/sentinel/main.py`
- `lifespan` context: create pool (min/max from config), set module pool holder, init LangFuse; teardown reverses.
- `require_token` dependency (compare `X-Sentinel-Token` to config) applied to webhooks/incidents/generate; `/health`+`/ready` exempt.
- Mount routers: webhooks, incidents, generate, health, eval.
- structlog bound with `incident_id` where applicable.

## Prerequisites
- [ ] tasks 1.2, 2.4, and the route tasks (5.1–5.4). [ ] eval router (Phase 6) may mount later — leave a stub or land after 6.x.

## Acceptance Criteria
- [ ] App boots cleanly (CLAUDE.md non-negotiable startup check) with pool + LangFuse initialized.
- [ ] Token enforced on protected routes; probes open.
- [ ] Graceful shutdown closes the pool (no leaked connections).

## Tests
- **Integration (`tests/test_api/test_app_boot.py`):** app starts; protected route 401 without token, 200 with; `/health` open; shutdown closes pool.
- **Boot check:** `uvicorn sentinel.main:app` starts with no import errors.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate — end of Category 3 Phase 5)
1. `uvicorn sentinel.main:app` boots; `/health` ok, protected routes require the token.
2. `pytest tests/test_api/ -q` green.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally._
