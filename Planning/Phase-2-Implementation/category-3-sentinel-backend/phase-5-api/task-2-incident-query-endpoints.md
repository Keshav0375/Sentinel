# task-2 — `GET /incidents*` query endpoints   ·   [category-3-sentinel-backend / phase-5-api]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-5-api` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §3.2 |
| **Depends on** | [[task-1-episodic-memory]], [[task-5-deploy-checker]] |
| **Referenced by** | [[task-3-ci-incident-response]] (poll), demo/inspection |

## Spec
Read endpoints over incident history.

**Files created/modified:** `src/sentinel/api/incidents.py`
- `GET /incidents?status=&service=&limit=` — filtered list.
- `GET /incidents/{id}` — single.
- `GET /incidents/{id}/trajectory` — full agent trace (JSONB).
- `GET /incidents/{id}/deploy` — linked `deployments` record.
- Token-protected; typed Pydantic responses.

## Prerequisites
- [ ] episodic memory (2.1), deploy_checker (3.5). [ ] token auth (5.5).

## Acceptance Criteria
- [ ] All four routes return the documented shapes; filters work; 401 without token; 404 for unknown id.

## Tests
- **Integration (`tests/test_api/test_incidents.py`):** seed incidents/deploys → list/filter/single/trajectory/deploy return correct data; auth enforced.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_api/test_incidents.py -q` green.
2. `curl .../incidents?limit=5` (with token) returns the seeded rows.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally._
