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

> ⚠ **rev-5 (2026-07-12):** auth changed. `X-Sentinel-Token`/`require_token` is **gone** —
> inbound auth is now Entra bearer, implemented in [[task-6-entra-bearer-auth]] (`require_incident_write`).
> This task now (a) wires the workload-identity Key Vault + PostgreSQL Entra-token access at
> startup, and (b) applies the **task-6** dependency to non-health routes. See sentinel §3.6 + §8.2.

## Spec
Wire the FastAPI app: lifespan (open asyncpg pool via **Entra DB token**, load LLM keys from
Key Vault via **workload identity**, init LangFuse at startup, close at shutdown), apply the
**Entra bearer** dependency (`require_incident_write`, task 5.6) to all non-health routes,
router registration, and structlog config.

**Files modified:** `src/sentinel/main.py`
- `lifespan` context: acquire Entra DB token + create pool (min/max from config), init KV client (workload identity), init LangFuse; teardown reverses.
- Apply `require_incident_write` (from `api/auth.py`, task 5.6) to webhooks/incidents/generate; `/health`+`/ready` exempt.
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
