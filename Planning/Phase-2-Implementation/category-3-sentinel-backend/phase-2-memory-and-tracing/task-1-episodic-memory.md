# task-1 — `memory/episodic.py` (asyncpg + pgvector)   ·   [category-3-sentinel-backend / phase-2-memory-and-tracing]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-2-memory-and-tracing` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §5.2, §5.3, §13.2 |
| **Depends on** | [[task-2-asyncpg-database-pool]], [[task-3-embeddings]] |
| **Referenced by** | [[task-3-incident-search]], [[task-1-webhook-receiver]] (store), [[task-1-judge-langfuse-scores]] |

## Spec
Rewrite episodic memory to asyncpg + pgvector, keeping the existing `MemoryStore` protocol
(`memory/base.py`) — same interface, new implementation (§13.2).

**Files modified:** `src/sentinel/memory/episodic.py`
- `store_incident(...)` → INSERT into `incidents` (trajectory JSONB, embedding VECTOR(384), all correlation fields, mttr, reflexion_loops).
- `search_similar(embedding, k)` → `ORDER BY embedding <=> $1 LIMIT k` (ivfflat).
- `get(incident_id)`, `list(status/service filters)`.
- Provenance preserved (which incident/agent) per safety invariant.

## Prerequisites
- [ ] tasks 1.2, 1.3, 2.3. [ ] local pgvector.

## Acceptance Criteria
- [ ] Implements the `MemoryStore` protocol unchanged; all methods async + typed.
- [ ] Similarity search uses the pgvector `<=>` operator (not numpy scan).
- [ ] Round-trips a full incident row incl. trajectory + embedding.

## Tests
- **Integration (`tests/test_memory/test_episodic.py`):** store 3 incidents, similarity search returns nearest; filter by status/service.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_memory/test_episodic.py -q` green against local pgvector.
2. Store + search returns the expected nearest incident.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally._
