# task-3 — Alembic setup + `001_initial_schema`   ·   [category-3-sentinel-backend / phase-1-data-layer]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-1-data-layer` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §5.3 (schema), §5.5, §13.3, §13.5 step 3 |
| **Depends on** | [[task-2-asyncpg-database-pool]] |
| **Referenced by** | [[task-4-seed-services-migration]], all memory/tool tasks |

## Spec
`alembic init` + the initial migration creating the extension, 3 tables, and indexes exactly
per §5.3.

**Files created:** `alembic.ini`, `alembic/env.py` (async engine), `alembic/versions/001_initial_schema.py`
- `CREATE EXTENSION IF NOT EXISTS vector`.
- `incidents` (all columns incl. `root_cause_confidence REAL`, `resolution_type`, `pr_number`, `pr_url`, `trajectory JSONB`, `eval_score JSONB`, `langfuse_trace_id`, `reflexion_loops`, `mttr_seconds`, `embedding VECTOR(384)`).
- `services` (name unique, team, tier, dependencies JSONB, runbook, metadata, embedding VECTOR(384)).
- `deployments` (service, pr_number, commit_sha, author, deploy_status, gha_run_id BIGINT, dd_deploy_event_id, files_changed JSONB, incident_id UUID FK→incidents, deployed_at, metadata).
- Indexes: status, service, correlation, ivfflat(incidents.embedding lists=10), ivfflat(services.embedding lists=5), deployments service/incident/commit.

## Prerequisites
- [ ] task 1.2 pool. [ ] local pgvector container.

## Acceptance Criteria
- [ ] `alembic upgrade head` on a fresh pgvector DB creates extension + 3 tables + all indexes; `downgrade` reverses.
- [ ] Column types/names match §5.3 exactly (conformance-checked).

## Tests
- **Integration (`tests/test_infra/test_migrations.py`):** upgrade against local container, assert tables/columns/indexes via `information_schema` + `pg_indexes`; downgrade clean.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `alembic upgrade head` on local pgvector → 3 tables.
2. `\d+ incidents` shows the vector column + ivfflat index.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally. Azure `alembic upgrade head` (infra §10 step 10) ⛔ B1 — separate from this task._
