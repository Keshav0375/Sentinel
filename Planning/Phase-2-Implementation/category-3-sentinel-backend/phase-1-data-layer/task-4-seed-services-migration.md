# task-4 — `002_seed_services` migration   ·   [category-3-sentinel-backend / phase-1-data-layer]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-1-data-layer` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §5.3 (services), §13.1 (replaces data/seed.py), §13.3 |
| **Depends on** | [[task-3-alembic-initial-schema]], [[task-3-embeddings]] (for embedding values — see note) |
| **Referenced by** | [[task-2-service-lookup]], triage flow |

## Spec
Seed the `services` table (replaces Phase-1 `data/services/*.json` + `seed.py`). Source the
service registry from the Phase-1 `service_map.json` content, mapped to columns: name, team,
tier, dependencies (JSONB), runbook (markdown), metadata, embedding(384).

**Files created:** `alembic/versions/002_seed_services.py`
- INSERT the known services with their dependencies/runbooks.
- Embedding: compute via the embedding model (all-MiniLM-L6-v2) at migration time, OR insert
  NULL and backfill in a follow-up if the model isn't importable in the migration context —
  **decide and document** (prefer computing so similarity search works immediately).

## Prerequisites
- [ ] task 1.3 schema. [ ] Phase-1 `data/services/*.json` still present (removed in Phase 9). [ ] embeddings util (task 2.3) if computing at seed time.

## Acceptance Criteria
- [ ] `alembic upgrade head` seeds all services with correct dependencies/runbooks/tier.
- [ ] Embeddings populated (or a documented backfill path), so `search services by symptom` returns matches.
- [ ] Idempotent-ish (ON CONFLICT (name) DO UPDATE or guarded).

## Tests
- **Integration:** upgrade → `SELECT count(*) FROM services` matches expected; a known symptom vector search returns the right service.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate — end of Category 3 Phase 1)
1. Fresh DB `alembic upgrade head` → services seeded.
2. Query one service by name; run a similarity search returning a sensible match.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Dependency on embeddings util (2.3) if computing at seed time — otherwise seed with backfill. Note the ordering choice in Report._
