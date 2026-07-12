# task-5 — `get_deploy_details` → PostgreSQL + GitHub API   ·   [category-3-sentinel-backend / phase-3-tools]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-3-tools` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §3 Tools, §4.8, §5.2 (deployments), §13.2 |
| **Depends on** | [[task-2-asyncpg-database-pool]], [[task-1-config-extensions]] |
| **Referenced by** | analysis agent, resolution (target deploy) |

## Spec
Rewrite `get_deploy_details` / `list_recent_deploys` to query the `deployments` table +
enrich with GitHub API (PR/commit) — this is the deploy↔incident correlation source.

**Files modified:** `src/sentinel/tools/deploy_checker.py`
- Input `DeployQuery{service, window_hours}` → Output list of `DeployRecord{pr_number, commit_sha, author, deploy_status, deployed_at, files_changed, ...}`.
- Postgres query "what deployed to this service in the last N hours?"; optional GitHub API enrichment via `github_api_token`.
- Read-only; structured errors; async.

## Prerequisites
- [ ] tasks 1.2, 3.1. [ ] local pgvector with sample `deployments` rows (fixture). [ ] GitHub token for enrichment (⛔ B9 for live PR fetch; mock in tests).

## Acceptance Criteria
- [ ] Returns recent deploys for a service ordered by time; failed deploys included (they're the correlation targets).
- [ ] GitHub enrichment optional + mockable; read-only; typed.

## Tests
- **Unit/integration (`tests/test_tools/test_deploy_checker.py`):** seed deployments, query window returns them; GitHub enrichment mocked.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_tools/test_deploy_checker.py -q` green.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live GitHub enrichment ⛔ B9. DB path + mocked tests writable now._
