# task-3 — `memory/embeddings.py` (pgvector `<=>`)   ·   [category-3-sentinel-backend / phase-2-memory-and-tracing]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-2-memory-and-tracing` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §13.2, §13.4 (numpy removal deferred) |
| **Depends on** | [[task-1-phase2-dependencies]] |
| **Referenced by** | [[task-1-episodic-memory]], [[task-2-semantic-memory]], [[task-4-seed-services-migration]] |

## Spec
Keep the local embedding model (all-MiniLM-L6-v2, 384-dim); move similarity to the DB
(pgvector `<=>`) instead of numpy cosine. This module now only produces embeddings.

**Files modified:** `src/sentinel/memory/embeddings.py`
- `embed(text) -> list[float]` (384) via sentence-transformers, model name from config.
- Remove numpy cosine helpers (numpy dep formally dropped in Phase 9; if ST needs it transitively, keep as transitive only).
- Cache the model instance; async-safe wrapper if the encode is sync (run in threadpool).

## Prerequisites
- [ ] task 1.1 deps. [ ] embedding model downloadable (first run pulls weights).

## Acceptance Criteria
- [ ] `embed()` returns a 384-float vector; deterministic for the same input.
- [ ] No cosine-in-Python similarity remains here (moved to pgvector in memory modules).

## Tests
- **Unit (`tests/test_memory/test_embeddings.py`):** vector length 384; two similar strings closer than a dissimilar one (compare via dot product for the assertion only).
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_memory/test_embeddings.py -q` green.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none (first run downloads model weights ~90 MB)._
