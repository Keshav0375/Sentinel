# task-2 — CI runner image (Dockerfile + build-push)   ·   [category-1-sentinel-infra / phase-4-wiring-and-ci]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `dev/infra-phase-4-wiring-and-ci` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §6 |
| **Depends on** | [[task-1-acr-module]] |
| **Referenced by** | [[task-3-infra-workflows]] (ci_runners), sentinel workflows (`container:` image) |

## Spec
Custom CI runner image (Python 3.12 + ruff/pyright/pytest + asyncpg/pgvector/alembic + az
CLI) so sentinel workflows don't reinstall tools each run.

**Files created:**
- `ci-images/ci-runner.Dockerfile` — per §6.1 (python:3.12-slim; apt curl gcc libpq-dev git docker.io; pip ruff pyright pytest pytest-asyncio asyncpg pgvector alembic; Azure CLI).
- `ci-images/build-push.sh` — `az acr login`, docker build, docker push `sentinelacr.azurecr.io/ci-runner:latest` (the one-time manual bootstrap, §6.2).

## Prerequisites
- [ ] Docker installed. [ ] task 2.1 ACR exists (⛔ B1 to push).

## Acceptance Criteria
- [ ] Image builds locally; contains ruff, pyright, pytest, az, kubectl-adjacent tools.
- [ ] `build-push.sh` tags/pushes `ci-runner:latest` to ACR.

## Tests
- **Build:** `docker build -f ci-images/ci-runner.Dockerfile .` succeeds; `docker run ... ruff --version && pyright --version && az version`.
- **Lint:** hadolint on the Dockerfile (optional); shellcheck build-push.sh.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. `docker build` succeeds; running the image prints tool versions.
2. (post-apply/ACR) `build-push.sh` pushes; `az acr repository show-tags --repository ci-runner` lists `latest`.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Push ⛔ B1 (ACR). Build + lint local now._
