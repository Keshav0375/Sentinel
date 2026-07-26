# task-2 — `memory/semantic.py` (asyncpg)   ·   [category-3-sentinel-backend / phase-2-memory-and-tracing]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-2-memory-and-tracing` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §5.2, §13.2 |
| **Depends on** | [[task-2-asyncpg-database-pool]], [[task-4-seed-services-migration]] |
| **Referenced by** | [[task-2-service-lookup]] |

## Spec
Rewrite semantic (service registry) memory to asyncpg PostgreSQL queries; same protocol.

**Files modified:** `src/sentinel/memory/semantic.py`
- `get_service(name)` → row from `services`.
- `search_services(embedding, k)` → pgvector similarity ("which service matches this symptom?").
- Typed dataclass/Pydantic return; async.

## Prerequisites
- [ ] tasks 1.2, 1.4 (seeded services). [ ] local pgvector.

## Acceptance Criteria
- [ ] Lookup-by-name + similarity search implemented against `services`.
- [ ] Protocol-compatible; no LLM call for lookup.

## Tests
- **Integration (`tests/test_memory/test_semantic.py`):** seed + lookup by name + symptom similarity returns expected service.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_memory/test_semantic.py -q` green.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally._
