# task-3 — `search_past_incidents` → pgvector   ·   [category-3-sentinel-backend / phase-3-tools]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-3-tools` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §3 Tools, §4.8, §5.5, §13.2 |
| **Depends on** | [[task-1-episodic-memory]], [[task-3-embeddings]], [[task-1-config-extensions]] |
| **Referenced by** | triage agent (dedup), analysis |

## Spec
Rewrite `search_past_incidents` to embed the symptom and query episodic memory via pgvector.

**Files modified:** `src/sentinel/tools/incident_search.py`
- Input `IncidentSearchInput{symptom_text, k}` → Output list of `SimilarIncident{id, root_cause, resolution_type, similarity}`.
- Read-only; uses injected episodic memory + embeddings.

## Prerequisites
- [ ] tasks 2.1, 2.3, 3.1. [ ] local pgvector with a few incidents (fixture).

## Acceptance Criteria
- [ ] Returns top-k similar incidents by embedding distance; empty list when none.
- [ ] Structured errors; typed; read-only.

## Tests
- **Unit/integration (`tests/test_tools/test_incident_search.py`):** seed incidents, search returns nearest by symptom; k respected.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_tools/test_incident_search.py -q` green.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally._
