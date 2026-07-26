# task-3 — `POST /generate/pr-content` endpoint   ·   [category-3-sentinel-backend / phase-5-api]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-5-api` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §3.4 |
| **Depends on** | [[task-4-pr-content-generator]] |
| **Referenced by** | [[task-3-ci-incident-response]] (generate-pr-content job) |

## Spec
The single endpoint that serves rollback PR content (demo PRs do NOT use it).

**Files created:** `src/sentinel/api/generate.py`
- `POST /generate/pr-content` — input per §3.4 (incident_id, root_cause, evidence_summary, confidence, target_deploy, resolution_type), token-protected → calls the `pr_content_generator` agent → `{title, description, model_used, tokens_used}`.

## Prerequisites
- [ ] task 4.4 agent. [ ] token auth (5.5). [ ] fake LLM for tests.

## Acceptance Criteria
- [ ] Endpoint returns the §3.4 response shape; title `revert:`-prefixed; 401 without token.
- [ ] Only wired to the agent (no PR creation here — GHA does that).

## Tests
- **Integration (`tests/test_api/test_generate.py`, fake LLM):** POST sample incident context → title + description present, model_used set; auth enforced.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_api/test_generate.py -q` green.
2. (with B4) live call returns a realistic revert PR body.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live call ⛔ B4. Endpoint + fake tests writable now._
