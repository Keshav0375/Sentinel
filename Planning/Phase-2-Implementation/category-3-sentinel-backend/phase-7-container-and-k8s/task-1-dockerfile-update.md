# task-1 — Dockerfile update (drop `data/`, torch-cpu)   ·   [category-3-sentinel-backend / phase-7-container-and-k8s]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-7-container-and-k8s` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §7, §13.2 (Dockerfile), §13.5 step 9 |
| **Depends on** | Phase-5 app boots |
| **Referenced by** | [[task-2-k8s-manifests]], [[task-2-ci-backend-deployment]] |

## Spec
Update the image: copy `src/` + `alembic/` (no `data/`), install with torch-cpu index to
keep the image small.

**Files modified:** `Dockerfile`
- `python:3.12-slim`; apt `libpq-dev gcc`; `pip install -r requirements.txt` with `PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu`; `COPY src/ alembic/ alembic.ini`; **no `COPY data/`**; `CMD uvicorn sentinel.main:app --host 0.0.0.0 --port 8000`.

## Prerequisites
- [ ] Docker installed. [ ] Phase-5 app importable.

## Acceptance Criteria
- [ ] `docker build` succeeds; image runs and serves `/health`; no `data/` in the image; torch is cpu build (~80 MB not ~2 GB).

## Tests
- **Build/run:** `docker build -t sentinel-backend:test .`; `docker run` + `curl /health` (with local DB env / FAKE_LLM).
- **Lint:** hadolint (optional).
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `docker build` succeeds; container serves `/health`.
2. `docker run … sh -c 'ls'` shows no `data/`.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally (Docker). Note: full boot needs DB env or FAKE_LLM._
