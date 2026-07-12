# task-1 — `ci_validation.yml` (fast PR gate)   ·   [category-3-sentinel-backend / phase-8-cicd]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-8-cicd` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §9.1, §9.5 |
| **Depends on** | Phase-5 app boots, [[task-1-dockerfile-update]] |
| **Referenced by** | PR gate for all future backend PRs |

## Spec
Fast PR gate — quality + single-job build/run/test (no ACR, no AKS, stubbed LLM). **Single
runner rule** (§9.1/§13.6): build image, run container, pgvector service, tests all in ONE
job.

**Files created:** `.github/workflows/ci_validation.yml` — name `[sentinel] PR — validation`; `pull_request` to main (paths src/tests/pyproject/Dockerfile/azure).
- `run-quality`: ruff check + `ruff format --check` + `pyright src/`.
- `build-and-test` (needs run-quality; service pgvector/pgvector:pg16): docker build (local, no push) → `docker run` with local DB + `SENTINEL_FAKE_LLM=1` → poll `/health`+`/ready` → `pytest test_tools+test_models` (unit) → `pytest test_agents+test_api` (integration) → teardown (`if: always()`).
- Can use the ACR ci-runner image as `container:` once available (infra 4.2) — optional.

## Prerequisites
- [ ] actionlint. [ ] Phase-5 app + Dockerfile (7.1).

## Acceptance Criteria
- [ ] Validates under actionlint; single-job build/run/test; no ACR/AKS/external calls; FAKE_LLM deterministic; ~3–5 min.

## Tests
- **Lint:** actionlint, yamllint.
- **Integration:** open a PR → the gate runs green on a clean branch.
- **Quality gate:** `--repo backend` (its steps mirror `quality_gate.py --repo backend`).

## How to Verify (phase gate)
1. actionlint clean.
2. A test PR runs the gate to green (quality + build + tests, no cloud).

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none for authoring; live PR run needs the repo on GitHub (present)._
